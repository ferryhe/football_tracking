from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

TARGET_QUALIFICATION_SCOPE = "target_finite_population"
TARGET_AUDIT_SCHEMA_VERSION = "1.0"
TARGET_AUDIT_PLAN_TYPE = "target_finite_population_audit_plan"
TARGET_AUDIT_LABELS_TYPE = "target_finite_population_audit_labels"
TARGET_QUALIFICATION_TYPE = "target_finite_population_qualification"
TARGET_QUALIFIED_APPLICATION_TYPE = "target_finite_population_qualified_application"
TARGET_PRELABEL_COMMITMENT_TYPE = "target_finite_population_prelabel_commitment"
SAMPLING_ALGORITHM = "sha256-target-derived-ordering-without-replacement-v2"
SAMPLING_ORDERING_SALT_DOMAIN = "football-tracking-target-audit-ordering-v1"
SAMPLING_SIZE_RULE = "conservative-true-ball-prevalence-floor-v1"
SAMPLING_TRUE_BALL_PREVALENCE_LOWER_BOUND = 0.10
SAMPLING_REQUIRED_DRAW_COUNT = 3680
ACCEPT_BOUND_ALGORITHM = "exact-hypergeometric-upper-with-binomial-floor-v1"
FALSE_REJECT_BOUND_ALGORITHM = "exact-binomial-upper-v1"
MIN_ACCEPTED_SUPPORT = 183
MIN_TRUE_BALL_SUPPORT = 368
ENDPOINT_ALPHA = 0.025
ACCEPT_ERROR_LIMIT = 0.02
FALSE_REJECT_LIMIT = 0.01
PLAN_COMMITMENT_ALGORITHM = "target-prelabel-plan-commitment-chain-v1"
EXTERNAL_COMMITMENT_ALGORITHM = "target-specific-exclusive-prelabel-commitment-v1"
MAX_TARGET_COMMITMENT_BYTES = 1024 * 1024
NON_LEAKAGE_SCAN_CHUNK_BYTES = 1024 * 1024
NON_LEAKAGE_MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
NON_LEAKAGE_MAX_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
NON_LEAKAGE_MAX_SECONDS = 120.0
NON_LEAKAGE_MAX_TOKEN_BYTES = 4096
NON_LEAKAGE_MAX_PROTECTED_TOKENS = 1_000_000
NON_LEAKAGE_TEXT_SUFFIXES = frozenset(
    {".cfg", ".csv", ".ini", ".json", ".jsonl", ".log", ".md", ".toml", ".tsv", ".txt", ".yaml", ".yml"}
)
NON_LEAKAGE_BINARY_SUFFIXES = frozenset(
    {
        ".avi",
        ".bin",
        ".jpeg",
        ".jpg",
        ".mkv",
        ".mov",
        ".mp4",
        ".npy",
        ".npz",
        ".onnx",
        ".png",
        ".pt",
        ".pth",
        ".webm",
    }
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STAGING_RECORD = re.compile(r"^\..+\.target-prelabel-commitment\.v1\.json\.[0-9a-f]{32}\.staging$")
_HEX_TOKEN_BYTES = re.compile(rb"[0-9A-Fa-f]+")
_CANDIDATE_TOKEN_BYTES = re.compile(rb"[A-Za-z0-9_.:-]+")
_TRUTH_FIELDS = frozenset(
    {
        "label",
        "truth",
        "ground_truth",
        "training_label",
        "annotation",
        "annotations",
        "resolution",
    }
)

_WINDOWS_FILE_LIST_DIRECTORY = 0x00000001
_WINDOWS_FILE_READ_ATTRIBUTES = 0x00000080
_WINDOWS_SYNCHRONIZE = 0x00100000
_WINDOWS_GENERIC_READ = 0x80000000
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_FILE_SHARE_WRITE = 0x00000002
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000


@dataclass(frozen=True)
class _WindowsDirectoryComponent:
    path: Path
    handle: int
    identity: tuple[int, int]


@dataclass(frozen=True)
class _WindowsDirectoryChain:
    components: tuple[_WindowsDirectoryComponent, ...]
_BINDING_FIELDS = frozenset(
    {
        "target_run_id",
        "source_sha256",
        "root_contract_sha256",
        "candidate_population_sha256",
        "model_sha256",
        "model_version",
        "confirmed_config_sha256",
        "policy_sha256",
        "policy_version",
        "thresholds_sha256",
    }
)
_HASH_BINDING_FIELDS = frozenset(
    {
        "source_sha256",
        "root_contract_sha256",
        "candidate_population_sha256",
        "model_sha256",
        "confirmed_config_sha256",
        "policy_sha256",
        "thresholds_sha256",
    }
)
_NOISE_LABELS = frozenset(
    {
        "player_body_or_shoe",
        "field_line_or_mark",
        "sideline_or_spare_ball",
        "equipment_or_background",
        "lighting_shadow_or_blur",
    }
)
_DECISION_ROW_FIELDS = frozenset(
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
_DECISION_VALUES = frozenset({"accept", "reject", "abstain"})
_DECISION_FORCED_REASONS = frozenset(
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


class TargetFinitePopulationError(ValueError):
    """A frozen target audit failed a fail-closed contract check."""


def exact_binomial_upper_bound(errors: int, total: int, *, alpha: float) -> float:
    """Return the exact one-sided Clopper-Pearson upper endpoint."""

    _validate_count_pair(errors, total)
    _validate_alpha(alpha)
    if total == 0:
        return 1.0
    if errors == total:
        return 1.0
    lower = errors / total
    upper = 1.0
    for _ in range(80):
        midpoint = (lower + upper) / 2.0
        if _binomial_lower_tail(errors, total, midpoint) > alpha:
            lower = midpoint
        else:
            upper = midpoint
    return upper


def hypergeometric_upper_bound(
    population_size: int,
    sample_size: int,
    errors: int,
    *,
    alpha: float,
) -> float:
    """Return an exact one-sided upper bound for a finite population error rate."""

    if (
        isinstance(population_size, bool)
        or not isinstance(population_size, int)
        or population_size <= 0
        or isinstance(sample_size, bool)
        or not isinstance(sample_size, int)
        or not 0 < sample_size <= population_size
    ):
        raise TargetFinitePopulationError("invalid hypergeometric population or sample size")
    _validate_count_pair(errors, sample_size)
    _validate_alpha(alpha)
    if sample_size == population_size:
        return errors / population_size

    lower = errors
    upper = population_size
    while lower < upper:
        midpoint = (lower + upper + 1) // 2
        if (
            _hypergeometric_lower_tail(
                errors,
                population_size=population_size,
                population_errors=midpoint,
                sample_size=sample_size,
            )
            >= alpha
        ):
            lower = midpoint
        else:
            upper = midpoint - 1
    return lower / population_size


def derive_target_bindings(
    frozen_application: Mapping[str, Any],
    *,
    target_run_id: str,
    confirmed_config_sha256: str,
) -> dict[str, str]:
    """Derive the complete target binding from validated frozen application evidence."""

    application = _validated_frozen_application(frozen_application)
    return _bindings_from_validated_application(
        application,
        target_run_id=target_run_id,
        confirmed_config_sha256=confirmed_config_sha256,
    )


def _bindings_from_validated_application(
    application: Mapping[str, Any],
    *,
    target_run_id: str,
    confirmed_config_sha256: str,
) -> dict[str, str]:
    evidence = application["target_binding_evidence"]
    return _validated_bindings(
        {
            "target_run_id": _required_text(target_run_id, "target_run_id"),
            "confirmed_config_sha256": _required_sha256(
                confirmed_config_sha256,
                "confirmed_config_sha256",
            ),
            **evidence,
        }
    )


def build_target_audit_plan(
    frozen_application: Mapping[str, Any],
    *,
    target_run_id: str,
    confirmed_config_sha256: str,
    commitment_root: Path,
) -> dict[str, Any]:
    """Freeze a label-independent exact-target audit before any labels are opened."""

    normalized_bindings = derive_target_bindings(
        frozen_application,
        target_run_id=target_run_id,
        confirmed_config_sha256=confirmed_config_sha256,
    )
    application = _validated_frozen_application(frozen_application)
    normalized_decisions = application["decisions"]
    normalized_population = [
        {
            "candidate_id": row["candidate_id"],
            "candidate_fingerprint": row["candidate_fingerprint"],
        }
        for row in normalized_decisions
    ]
    expected_population_sha256 = normalized_bindings["candidate_population_sha256"]
    target_identity = _target_commitment_identity(
        application_content_sha256=application["application_content_sha256"],
        bindings=normalized_bindings,
        population_sha256=expected_population_sha256,
    )
    target_key = _canonical_sha256(target_identity)
    ordering_salt = _canonical_sha256(
        {
            "domain": SAMPLING_ORDERING_SALT_DOMAIN,
            "target_key": target_key,
        }
    )
    sample_size = min(len(normalized_population), SAMPLING_REQUIRED_DRAW_COUNT)
    sample_size_rule = _sampling_size_rule(
        population_size=len(normalized_population),
        sample_size=sample_size,
    )
    sampling_design = {
        "algorithm": SAMPLING_ALGORITHM,
        "mode": "fixed",
        "without_replacement": True,
        "label_independent": True,
        "sample_size": sample_size,
        "ordering_salt_domain": SAMPLING_ORDERING_SALT_DOMAIN,
        "ordering_salt": ordering_salt,
        "sample_size_rule": sample_size_rule,
        "population_sha256": expected_population_sha256,
        "binding_sha256": _canonical_sha256(normalized_bindings),
    }
    sampling_design_sha256 = _canonical_sha256(sampling_design)
    sample = _ranked_sample(
        normalized_population,
        normalized_decisions,
        sample_size=sample_size,
        ordering_salt=ordering_salt,
    )
    sample_sha256 = _canonical_sha256(sample)
    commitment = {
        "algorithm": PLAN_COMMITMENT_ALGORITHM,
        "sequence": 1,
        "previous_commitment_sha256": None,
        "status": "committed_before_labels",
        "target_run_id": normalized_bindings["target_run_id"],
        "frozen_application_content_sha256": application["application_content_sha256"],
        "bindings_sha256": _canonical_sha256(normalized_bindings),
        "sampling_design_sha256": sampling_design_sha256,
        "sample_sha256": sample_sha256,
    }
    content = {
        "schema_version": TARGET_AUDIT_SCHEMA_VERSION,
        "artifact_type": TARGET_AUDIT_PLAN_TYPE,
        "qualification_scope": TARGET_QUALIFICATION_SCOPE,
        "status": "frozen_before_labels",
        "training_eligible": False,
        "reusable": False,
        "promotion_scope": "exact_target_only",
        "bindings": normalized_bindings,
        "frozen_application_content_sha256": application["application_content_sha256"],
        "population": normalized_population,
        "frozen_decisions": normalized_decisions,
        "sampling_design": sampling_design,
        "sampling_design_sha256": sampling_design_sha256,
        "sample": sample,
        "sample_sha256": sample_sha256,
        "plan_commitment": commitment,
        "plan_commitment_sha256": _canonical_sha256(commitment),
    }
    plan_sha256 = _canonical_sha256(content)
    record = {
        "schema_version": TARGET_AUDIT_SCHEMA_VERSION,
        "artifact_type": TARGET_PRELABEL_COMMITMENT_TYPE,
        "qualification_scope": TARGET_QUALIFICATION_SCOPE,
        "status": "committed_before_labels",
        "algorithm": EXTERNAL_COMMITMENT_ALGORITHM,
        "target_key": target_key,
        "target_identity": target_identity,
        "plan_sha256": plan_sha256,
        "plan_commitment_sha256": content["plan_commitment_sha256"],
        "ordering_salt": ordering_salt,
        "sample_size_rule": sample_size_rule,
        "sampling_design_sha256": sampling_design_sha256,
        "sample_sha256": sample_sha256,
    }
    record_bytes = _json_bytes(record)
    record_sha256 = hashlib.sha256(record_bytes).hexdigest()
    record_name = f"{target_key}.target-prelabel-commitment.v1.json"
    record_path = _publish_exclusive_commitment_record(
        Path(commitment_root),
        record_name=record_name,
        record_bytes=record_bytes,
    )
    external_commitment = {
        "artifact_type": TARGET_PRELABEL_COMMITMENT_TYPE,
        "target_key": target_key,
        "record_name": record_path.name,
        "record_sha256": record_sha256,
    }
    return {
        **content,
        "external_commitment": external_commitment,
        "plan_sha256": plan_sha256,
    }


def target_prelabel_commitment_path(
    commitment_root: Path,
    plan: Mapping[str, Any],
) -> Path:
    descriptor = _required_mapping(plan.get("external_commitment"), "external_commitment")
    record_name = _safe_record_name(descriptor.get("record_name"))
    return Path(os.path.abspath(commitment_root)) / record_name


def validate_target_prelabel_commitment(
    plan: Mapping[str, Any],
    commitment_path: Path,
) -> dict[str, Any]:
    """Validate the externally published target-specific pre-label commitment."""

    normalized = _validated_plan(plan, commitment_path=commitment_path)
    return dict(normalized["external_commitment"])


def validate_target_prelabel_commitment_bytes(
    plan: Mapping[str, Any],
    commitment_bytes: bytes,
    *,
    record_name: str,
) -> dict[str, Any]:
    """Validate one already captured canonical commitment record without reopening it."""

    normalized = _validated_plan(
        plan,
        commitment_bytes=commitment_bytes,
        commitment_record_name=record_name,
    )
    return dict(normalized["external_commitment"])


def evaluate_target_audit(
    plan: Mapping[str, Any],
    labels_path: Path,
    *,
    commitment_path: Path,
) -> dict[str, Any]:
    """Evaluate complete blind labels without emitting or reusing target truth."""

    normalized_plan = _validated_plan(plan, commitment_path=commitment_path)
    normalized_labels, labels_manifest = _validated_labels(
        labels_path,
        normalized_plan,
        commitment_path=commitment_path,
    )
    sample = normalized_plan["sample"]
    labels_by_id = {row["candidate_id"]: row for row in normalized_labels}
    accepted_population_size = sum(row["decision"] == "accept" for row in normalized_plan["frozen_decisions"])
    accepted_sample = [row for row in sample if row["decision"] == "accept"]
    accepted_errors = sum(labels_by_id[row["candidate_id"]]["label"] != "match_ball" for row in accepted_sample)
    true_ball_sample = [row for row in sample if labels_by_id[row["candidate_id"]]["label"] == "match_ball"]
    false_reject_errors = sum(row["decision"] == "reject" for row in true_ball_sample)
    accepted_finite_upper = (
        hypergeometric_upper_bound(
            accepted_population_size,
            len(accepted_sample),
            accepted_errors,
            alpha=ENDPOINT_ALPHA,
        )
        if accepted_population_size and accepted_sample
        else 1.0
    )
    accepted_binomial_upper = exact_binomial_upper_bound(
        accepted_errors,
        len(accepted_sample),
        alpha=ENDPOINT_ALPHA,
    )
    false_reject_upper = exact_binomial_upper_bound(
        false_reject_errors,
        len(true_ball_sample),
        alpha=ENDPOINT_ALPHA,
    )
    accepted_support = len(accepted_sample) >= MIN_ACCEPTED_SUPPORT
    true_ball_support = len(true_ball_sample) >= MIN_TRUE_BALL_SUPPORT
    accepted_bound_passed = (
        accepted_finite_upper <= ACCEPT_ERROR_LIMIT and accepted_binomial_upper <= ACCEPT_ERROR_LIMIT
    )
    false_reject_bound_passed = false_reject_upper <= FALSE_REJECT_LIMIT
    qualified = accepted_support and true_ball_support and accepted_bound_passed and false_reject_bound_passed
    label_content_sha256 = _sha256_file(Path(labels_path))
    content = {
        "schema_version": TARGET_AUDIT_SCHEMA_VERSION,
        "artifact_type": TARGET_QUALIFICATION_TYPE,
        "qualification_scope": TARGET_QUALIFICATION_SCOPE,
        "status": "qualified" if qualified else "review_only",
        "training_eligible": False,
        "reusable": False,
        "promotion_scope": "exact_target_only",
        "bindings": normalized_plan["bindings"],
        "plan_sha256": normalized_plan["plan_sha256"],
        "external_commitment_sha256": normalized_plan["external_commitment"]["record_sha256"],
        "sampling_design_sha256": normalized_plan["sampling_design_sha256"],
        "audit_labels_sha256": label_content_sha256,
        "annotation_resolution_sha256": labels_manifest["annotation_package"]["resolution_sha256"],
        "vote_ledger_sha256": labels_manifest["annotation_package"]["ledger_sha256"],
        "sample_sha256": normalized_plan["sample_sha256"],
        "support": {
            "accepted": {
                "observed": len(accepted_sample),
                "minimum": MIN_ACCEPTED_SUPPORT,
                "passed": accepted_support,
            },
            "true_balls": {
                "observed": len(true_ball_sample),
                "minimum": MIN_TRUE_BALL_SUPPORT,
                "passed": true_ball_support,
            },
        },
        "endpoints": {
            "accepted_precision": {
                "population_size": accepted_population_size,
                "sample_size": len(accepted_sample),
                "error_count": accepted_errors,
                "alpha": ENDPOINT_ALPHA,
                "risk_limit": ACCEPT_ERROR_LIMIT,
                "finite_population_upper": accepted_finite_upper,
                "binomial_reconciliation_upper": accepted_binomial_upper,
                "method": ACCEPT_BOUND_ALGORITHM,
                "passed": accepted_bound_passed,
            },
            "false_reject": {
                "sample_size": len(true_ball_sample),
                "error_count": false_reject_errors,
                "alpha": ENDPOINT_ALPHA,
                "risk_limit": FALSE_REJECT_LIMIT,
                "binomial_upper": false_reject_upper,
                "method": FALSE_REJECT_BOUND_ALGORITHM,
                "passed": false_reject_bound_passed,
            },
        },
    }
    return {**content, "qualification_sha256": _canonical_sha256(content)}


def validate_target_qualification(
    qualification: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    expected_bindings: Mapping[str, Any],
    commitment_path: Path,
    labels_path: Path | None = None,
) -> None:
    """Validate exact-target use; this artifact is never a reusable/global promotion."""

    normalized_plan = _validated_plan(plan, commitment_path=commitment_path)
    expected = _validated_bindings(expected_bindings)
    if normalized_plan["bindings"] != expected:
        raise TargetFinitePopulationError("target qualification binding does not match the requested target")
    if not isinstance(qualification, Mapping):
        raise TargetFinitePopulationError("target qualification must be an object")
    content = dict(qualification)
    qualification_sha256 = content.pop("qualification_sha256", None)
    if qualification_sha256 != _canonical_sha256(content):
        raise TargetFinitePopulationError("target qualification hash is invalid")
    if (
        content.get("schema_version") != TARGET_AUDIT_SCHEMA_VERSION
        or content.get("artifact_type") != TARGET_QUALIFICATION_TYPE
        or content.get("qualification_scope") != TARGET_QUALIFICATION_SCOPE
        or content.get("bindings") != expected
        or content.get("plan_sha256") != normalized_plan["plan_sha256"]
        or content.get("external_commitment_sha256")
        != normalized_plan["external_commitment"]["record_sha256"]
        or content.get("sampling_design_sha256") != normalized_plan["sampling_design_sha256"]
        or content.get("training_eligible") is not False
        or content.get("reusable") is not False
        or content.get("promotion_scope") != "exact_target_only"
        or content.get("status") not in {"qualified", "review_only"}
    ):
        raise TargetFinitePopulationError("target qualification envelope or binding is invalid")
    if labels_path is not None:
        expected_qualification = evaluate_target_audit(
            normalized_plan,
            labels_path,
            commitment_path=commitment_path,
        )
        if qualification != expected_qualification:
            raise TargetFinitePopulationError("target qualification does not recompute from the frozen plan and labels")


def build_target_qualified_application(
    frozen_application: Mapping[str, Any],
    plan: Mapping[str, Any],
    labels_path: Path,
    qualification: Mapping[str, Any],
    *,
    commitment_path: Path,
) -> dict[str, Any]:
    """Activate decisions only for the exact target after the bound audit qualifies."""

    normalized_plan = _validated_plan(plan, commitment_path=commitment_path)
    validate_target_qualification(
        qualification,
        normalized_plan,
        expected_bindings=normalized_plan["bindings"],
        commitment_path=commitment_path,
        labels_path=labels_path,
    )
    if qualification.get("status") != "qualified":
        raise TargetFinitePopulationError("target qualification remains review_only")
    application = _validated_frozen_application(frozen_application)
    expected_bindings = _bindings_from_validated_application(
        application,
        target_run_id=normalized_plan["bindings"]["target_run_id"],
        confirmed_config_sha256=normalized_plan["bindings"]["confirmed_config_sha256"],
    )
    if expected_bindings != normalized_plan["bindings"]:
        raise TargetFinitePopulationError("target audit plan does not match the frozen application binding evidence")
    if (
        application["application_content_sha256"] != normalized_plan["frozen_application_content_sha256"]
        or application["decisions"] != normalized_plan["frozen_decisions"]
    ):
        raise TargetFinitePopulationError("target frozen application decisions do not match the predeclared plan")
    application_content_sha256 = application["application_content_sha256"]
    content = {
        "schema_version": TARGET_AUDIT_SCHEMA_VERSION,
        "artifact_type": TARGET_QUALIFIED_APPLICATION_TYPE,
        "qualification_scope": TARGET_QUALIFICATION_SCOPE,
        "status": "target_qualified_policy_applied",
        "training_eligible": False,
        "reusable": False,
        "promotion_scope": "exact_target_only",
        "bindings": normalized_plan["bindings"],
        "policy_version": application.get("policy_version"),
        "dataset_version": application.get("dataset_version"),
        "model_version": application.get("model_version"),
        "lineage": application.get("lineage"),
        "plan_sha256": normalized_plan["plan_sha256"],
        "external_commitment_sha256": normalized_plan["external_commitment"]["record_sha256"],
        "qualification_sha256": qualification.get("qualification_sha256"),
        "frozen_application_content_sha256": application_content_sha256,
        "summary": application.get("summary"),
        "decisions": list(application["decisions"]),
    }
    return {**content, "application_content_sha256": _canonical_sha256(content)}


def _validated_plan(
    plan: Mapping[str, Any],
    *,
    commitment_path: Path | None = None,
    commitment_bytes: bytes | None = None,
    commitment_record_name: str | None = None,
) -> dict[str, Any]:
    if (commitment_path is None) == (commitment_bytes is None):
        raise TargetFinitePopulationError(
            "exactly one external commitment path or captured record is required"
        )
    if not isinstance(plan, Mapping):
        raise TargetFinitePopulationError("target audit plan must be an object")
    content = dict(plan)
    plan_sha256 = content.pop("plan_sha256", None)
    external_commitment = _required_mapping(
        content.pop("external_commitment", None),
        "external_commitment",
    )
    if plan_sha256 != _canonical_sha256(content):
        raise TargetFinitePopulationError("target audit plan hash is invalid")
    if (
        content.get("schema_version") != TARGET_AUDIT_SCHEMA_VERSION
        or content.get("artifact_type") != TARGET_AUDIT_PLAN_TYPE
        or content.get("qualification_scope") != TARGET_QUALIFICATION_SCOPE
        or content.get("status") != "frozen_before_labels"
        or content.get("training_eligible") is not False
        or content.get("reusable") is not False
        or content.get("promotion_scope") != "exact_target_only"
    ):
        raise TargetFinitePopulationError("target audit plan envelope is invalid")
    bindings = _validated_bindings(_required_mapping(content.get("bindings"), "bindings"))
    population = _normalized_population(_required_sequence(content.get("population"), "population"))
    decisions = _normalized_decisions(
        _required_sequence(content.get("frozen_decisions"), "frozen_decisions"),
        population,
    )
    if bindings["candidate_population_sha256"] != _canonical_sha256(population):
        raise TargetFinitePopulationError("target audit plan population binding is invalid")
    frozen_application_content_sha256 = _required_sha256(
        content.get("frozen_application_content_sha256"),
        "frozen_application_content_sha256",
    )
    target_identity = _target_commitment_identity(
        application_content_sha256=frozen_application_content_sha256,
        bindings=bindings,
        population_sha256=bindings["candidate_population_sha256"],
    )
    ordering_salt = _canonical_sha256(
        {
            "domain": SAMPLING_ORDERING_SALT_DOMAIN,
            "target_key": _canonical_sha256(target_identity),
        }
    )
    sample_size = min(len(population), SAMPLING_REQUIRED_DRAW_COUNT)
    sample_size_rule = _sampling_size_rule(
        population_size=len(population),
        sample_size=sample_size,
    )
    design = _required_mapping(content.get("sampling_design"), "sampling_design")
    design_sha256 = _required_sha256(content.get("sampling_design_sha256"), "sampling_design_sha256")
    expected_design = {
        "algorithm": SAMPLING_ALGORITHM,
        "mode": "fixed",
        "without_replacement": True,
        "label_independent": True,
        "sample_size": sample_size,
        "ordering_salt_domain": SAMPLING_ORDERING_SALT_DOMAIN,
        "ordering_salt": ordering_salt,
        "sample_size_rule": sample_size_rule,
        "population_sha256": bindings["candidate_population_sha256"],
        "binding_sha256": _canonical_sha256(bindings),
    }
    if design != expected_design or _canonical_sha256(expected_design) != design_sha256:
        raise TargetFinitePopulationError("target audit sampling design is invalid")
    sample = _required_sequence(content.get("sample"), "sample")
    if content.get("sample_sha256") != _canonical_sha256(sample):
        raise TargetFinitePopulationError("target audit sample hash is invalid")
    if len(sample) != sample_size:
        raise TargetFinitePopulationError("target audit sample size is invalid")
    expected_sample = _ranked_sample(
        population,
        decisions,
        sample_size=sample_size,
        ordering_salt=ordering_salt,
    )
    if sample != expected_sample:
        raise TargetFinitePopulationError("target audit sample does not match the committed hash-ranked ordering")
    population_by_id = {row["candidate_id"]: row for row in population}
    decisions_by_id = {row["candidate_id"]: row for row in decisions}
    seen: set[str] = set()
    for index, row in enumerate(sample):
        if not isinstance(row, Mapping):
            raise TargetFinitePopulationError("target audit sample row is invalid")
        candidate_id = _required_text(row.get("candidate_id"), "sample candidate_id")
        if candidate_id in seen:
            raise TargetFinitePopulationError("target audit sample contains duplicate candidates")
        seen.add(candidate_id)
        expected = population_by_id.get(candidate_id)
        if (
            row.get("order") != index
            or expected is None
            or row.get("candidate_fingerprint") != expected["candidate_fingerprint"]
            or row.get("decision") != decisions_by_id[candidate_id]["decision"]
            or set(row) != {"order", "candidate_id", "candidate_fingerprint", "decision"}
        ):
            raise TargetFinitePopulationError("target audit sample order or binding is invalid")
    commitment = _required_mapping(content.get("plan_commitment"), "plan_commitment")
    commitment_sha256 = _required_sha256(
        content.get("plan_commitment_sha256"),
        "plan_commitment_sha256",
    )
    expected_commitment = {
        "algorithm": PLAN_COMMITMENT_ALGORITHM,
        "sequence": 1,
        "previous_commitment_sha256": None,
        "status": "committed_before_labels",
        "target_run_id": bindings["target_run_id"],
        "frozen_application_content_sha256": frozen_application_content_sha256,
        "bindings_sha256": _canonical_sha256(bindings),
        "sampling_design_sha256": design_sha256,
        "sample_sha256": content["sample_sha256"],
    }
    if commitment != expected_commitment or commitment_sha256 != _canonical_sha256(expected_commitment):
        raise TargetFinitePopulationError("target audit pre-label plan commitment is invalid")
    normalized = {
        **content,
        "external_commitment": dict(external_commitment),
        "plan_sha256": plan_sha256,
    }
    if commitment_path is not None:
        captured_bytes = _read_regular_file_bytes(
            Path(commitment_path),
            "target pre-label commitment",
        )
        captured_name = Path(commitment_path).resolve().name
    else:
        captured_bytes = bytes(commitment_bytes)
        captured_name = _safe_record_name(commitment_record_name)
    _validate_external_commitment_bytes(
        normalized,
        captured_bytes,
        record_name=captured_name,
    )
    return normalized


def build_target_audit_labels_from_annotation_package(
    plan: Mapping[str, Any],
    *,
    package_root: Path,
    contract_path: Path,
    ledger_path: Path,
    dataset_manifest_path: Path,
    annotation_resolution_path: Path,
    commitment_path: Path,
    previous_ledger_path: Path | None = None,
) -> dict[str, Any]:
    """Project target-only labels from a replayed human annotation package."""

    normalized_plan = _validated_plan(plan, commitment_path=commitment_path)
    root = Path(package_root).resolve()
    paths = {
        "contract": _contained_package_file(root, contract_path, "target annotation contract"),
        "ledger": _contained_package_file(root, ledger_path, "target annotation ledger"),
        "dataset_manifest": _contained_package_file(
            root,
            dataset_manifest_path,
            "target annotation dataset manifest",
        ),
        "resolution": _contained_package_file(
            root,
            annotation_resolution_path,
            "target annotation resolution",
        ),
    }
    if previous_ledger_path is not None:
        paths["previous_ledger"] = _contained_package_file(
            root,
            previous_ledger_path,
            "target annotation previous ledger",
        )
    rows = _replay_target_annotation_package(normalized_plan, paths)
    package = {
        f"{name}_path": path.relative_to(root).as_posix()
        for name, path in paths.items()
    }
    package.update({f"{name}_sha256": _sha256_file(path) for name, path in paths.items()})
    return {
        "schema_version": TARGET_AUDIT_SCHEMA_VERSION,
        "artifact_type": TARGET_AUDIT_LABELS_TYPE,
        "qualification_scope": TARGET_QUALIFICATION_SCOPE,
        "training_eligible": False,
        "calibration_eligible": False,
        "reusable": False,
        "plan_sha256": normalized_plan["plan_sha256"],
        "external_commitment_sha256": normalized_plan["external_commitment"]["record_sha256"],
        "plan_commitment_sha256": normalized_plan["plan_commitment_sha256"],
        "sampling_design_sha256": normalized_plan["sampling_design_sha256"],
        "sample_sha256": normalized_plan["sample_sha256"],
        "annotation_package": package,
        "labels": rows,
    }


def validate_target_label_non_leakage(
    labels_path: Path,
    consumer_artifact_paths: Sequence[Path],
    *,
    plan_path: Path,
    commitment_path: Path,
) -> None:
    """Reject target truth copied into any non-target consumer artifact."""

    deadline = time.monotonic() + NON_LEAKAGE_MAX_SECONDS
    scanned_bytes = [0]
    labels_file = Path(labels_path)
    plan_file = Path(plan_path)
    plan = _validated_plan(
        _load_json_file(plan_file, "target audit plan"),
        commitment_path=commitment_path,
    )
    labels, manifest = _validated_labels(
        labels_file,
        plan,
        commitment_path=commitment_path,
    )
    package = _required_mapping(manifest.get("annotation_package"), "annotation_package")
    protected_hashes = {
        _sha256_file(labels_file),
        _sha256_file(plan_file),
        plan["plan_sha256"],
        plan["external_commitment"]["record_sha256"],
        plan["plan_commitment_sha256"],
        plan["sampling_design_sha256"],
        plan["sample_sha256"],
    }
    protected_hashes.update(
        _required_sha256(value, f"annotation_package.{name}")
        for name, value in package.items()
        if name.endswith("_sha256")
    )
    protected_hashes.update(
        value
        for row in labels
        for value in (row["candidate_fingerprint"], row["evidence_sha256"])
    )
    protected_candidate_ids = {row["candidate_id"] for row in plan["population"]}
    protected_hashes.update(row["candidate_fingerprint"] for row in plan["population"])
    encoded_candidate_ids = _encoded_candidate_id_tokens(protected_candidate_ids)

    package_root = labels_file.parent.resolve()
    for name, value in package.items():
        if not name.endswith("_path"):
            continue
        package_path = _contained_package_file(
            package_root,
            package_root / _safe_relative_path(value, f"annotation_package.{name}"),
            f"target annotation {name.removesuffix('_path')}",
        )
        digest, discovered_hashes, _referenced = _stream_target_tokens(
            package_path,
            protected_hashes=frozenset(),
            protected_candidate_ids=frozenset(),
            collect_hashes=True,
            deadline=deadline,
            scanned_bytes=scanned_bytes,
        )
        expected_sha256 = _required_sha256(
            package.get(f"{name.removesuffix('_path')}_sha256"),
            f"annotation_package.{name.removesuffix('_path')}_sha256",
        )
        if digest != expected_sha256:
            raise TargetFinitePopulationError(
                f"target annotation {name.removesuffix('_path')} changed during non-leakage validation"
            )
        protected_hashes.update(discovered_hashes)
        if len(protected_hashes) > NON_LEAKAGE_MAX_PROTECTED_TOKENS:
            raise TargetFinitePopulationError(
                "target non-leakage protected token set exceeds the configured limit"
            )

    for path in consumer_artifact_paths:
        scan_mode = _non_leakage_scan_mode(Path(path))
        if scan_mode == "lineage_only":
            continue
        _digest, _discovered, referenced = _stream_target_tokens(
            Path(path),
            protected_hashes=frozenset(value.encode("ascii") for value in protected_hashes),
            protected_candidate_ids=encoded_candidate_ids,
            collect_hashes=False,
            deadline=deadline,
            scanned_bytes=scanned_bytes,
        )
        if referenced:
            raise TargetFinitePopulationError(
                f"target audit labels are referenced by training or calibration artifact {Path(path).name!r}"
            )


def _non_leakage_scan_mode(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in NON_LEAKAGE_TEXT_SUFFIXES:
        return "text"
    if suffix in NON_LEAKAGE_BINARY_SUFFIXES:
        return "lineage_only"
    raise TargetFinitePopulationError(
        f"declared non-target artifact {path.name!r} has no approved content classification"
    )


def _encoded_candidate_id_tokens(candidate_ids: set[str]) -> frozenset[bytes]:
    encoded: set[bytes] = set()
    for candidate_id in candidate_ids:
        try:
            token = candidate_id.encode("ascii")
        except UnicodeEncodeError as exc:
            raise TargetFinitePopulationError(
                "target candidate ids must use the bounded non-leakage token alphabet"
            ) from exc
        if (
            len(token) > NON_LEAKAGE_MAX_TOKEN_BYTES
            or _CANDIDATE_TOKEN_BYTES.fullmatch(token) is None
        ):
            raise TargetFinitePopulationError(
                "target candidate ids must use the bounded non-leakage token alphabet"
            )
        encoded.add(token)
    return frozenset(encoded)


def _stream_target_tokens(
    path: Path,
    *,
    protected_hashes: frozenset[bytes],
    protected_candidate_ids: frozenset[bytes],
    collect_hashes: bool,
    deadline: float,
    scanned_bytes: list[int],
) -> tuple[str, set[str], bool]:
    target = Path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = (
            _open_windows_registry_entry(
                target,
                label=f"target non-leakage artifact {target.name!r}",
            )
            if os.name == "nt"
            else os.open(target, flags)
        )
    except OSError as exc:
        raise TargetFinitePopulationError(
            f"target non-leakage artifact {target.name!r} is unavailable"
        ) from exc
    digest = hashlib.sha256()
    hash_lexer = _BoundedTokenLexer(
        _HEX_TOKEN_BYTES,
        max_token_bytes=64,
        protected=frozenset(token.lower() for token in protected_hashes),
        collect=collect_hashes,
        normalize=bytes.lower,
    )
    candidate_lexer = _BoundedTokenLexer(
        _CANDIDATE_TOKEN_BYTES,
        max_token_bytes=max((len(token) for token in protected_candidate_ids), default=1),
        protected=protected_candidate_ids,
        collect=False,
        normalize=lambda token: token,
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise TargetFinitePopulationError(
                f"target non-leakage artifact {target.name!r} must be a regular file"
            )
        if before.st_size > NON_LEAKAGE_MAX_FILE_BYTES:
            raise TargetFinitePopulationError(
                f"target non-leakage artifact {target.name!r} exceeds the scan size limit"
            )
        while True:
            if time.monotonic() > deadline:
                raise TargetFinitePopulationError(
                    "target non-leakage scan exceeded the configured time limit"
                )
            chunk = os.read(descriptor, NON_LEAKAGE_SCAN_CHUNK_BYTES)
            if not chunk:
                break
            scanned_bytes[0] += len(chunk)
            if scanned_bytes[0] > NON_LEAKAGE_MAX_TOTAL_BYTES:
                raise TargetFinitePopulationError(
                    "target non-leakage scan exceeded the configured byte budget"
                )
            digest.update(chunk)
            hash_lexer.feed(chunk)
            candidate_lexer.feed(chunk)
        hash_lexer.finish()
        candidate_lexer.finish()
        after = os.fstat(descriptor)
        try:
            path_after = os.stat(target, follow_symlinks=False)
        except OSError as exc:
            raise TargetFinitePopulationError(
                f"target non-leakage artifact {target.name!r} changed during scanning"
            ) from exc
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or (
            path_after.st_dev != after.st_dev
            or path_after.st_ino != after.st_ino
            or path_after.st_mode != after.st_mode
            or path_after.st_size != after.st_size
        ):
            raise TargetFinitePopulationError(
                f"target non-leakage artifact {target.name!r} changed during scanning"
            )
    finally:
        os.close(descriptor)
    return (
        digest.hexdigest(),
        {token.decode("ascii") for token in hash_lexer.collected},
        hash_lexer.matched or candidate_lexer.matched,
    )


class _BoundedTokenLexer:
    def __init__(
        self,
        pattern: re.Pattern[bytes],
        *,
        max_token_bytes: int,
        protected: frozenset[bytes],
        collect: bool,
        normalize: Any,
    ) -> None:
        self._pattern = pattern
        self._max_token_bytes = max_token_bytes
        self._protected = protected
        self._collect = collect
        self._normalize = normalize
        self._carry = b""
        self._overflow = False
        self.collected: set[bytes] = set()
        self.matched = False

    def feed(self, chunk: bytes, *, final: bool = False) -> None:
        data = self._carry + chunk
        continuation_overflow = self._overflow
        self._carry = b""
        self._overflow = False
        for match in self._pattern.finditer(data):
            at_end = match.end() == len(data) and not final
            if continuation_overflow and match.start() == 0:
                if at_end:
                    self._overflow = True
                continuation_overflow = False
                continue
            token = match.group()
            if at_end:
                if len(token) > self._max_token_bytes:
                    self._overflow = True
                else:
                    self._carry = token
                continue
            if len(token) > self._max_token_bytes:
                continue
            normalized = self._normalize(token)
            if normalized in self._protected:
                self.matched = True
            if self._collect and len(token) == self._max_token_bytes:
                self.collected.add(normalized)
                if len(self.collected) > NON_LEAKAGE_MAX_PROTECTED_TOKENS:
                    raise TargetFinitePopulationError(
                        "target non-leakage protected token set exceeds the configured limit"
                    )

    def finish(self) -> None:
        self.feed(b"", final=True)


def _validated_labels(
    labels_path: Path,
    plan: Mapping[str, Any],
    *,
    commitment_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = Path(labels_path)
    manifest = _load_json_file(path, "target audit labels")
    if set(manifest) != {
        "schema_version",
        "artifact_type",
        "qualification_scope",
        "training_eligible",
        "calibration_eligible",
        "reusable",
        "plan_sha256",
        "external_commitment_sha256",
        "plan_commitment_sha256",
        "sampling_design_sha256",
        "sample_sha256",
        "annotation_package",
        "labels",
    }:
        raise TargetFinitePopulationError("target audit labels contain unsupported training, adaptive, or extra fields")
    if (
        manifest.get("schema_version") != TARGET_AUDIT_SCHEMA_VERSION
        or manifest.get("artifact_type") != TARGET_AUDIT_LABELS_TYPE
        or manifest.get("qualification_scope") != TARGET_QUALIFICATION_SCOPE
        or manifest.get("training_eligible") is not False
        or manifest.get("calibration_eligible") is not False
        or manifest.get("reusable") is not False
        or manifest.get("plan_sha256") != plan["plan_sha256"]
        or manifest.get("external_commitment_sha256")
        != plan["external_commitment"]["record_sha256"]
        or manifest.get("plan_commitment_sha256") != plan["plan_commitment_sha256"]
        or manifest.get("sampling_design_sha256") != plan["sampling_design_sha256"]
        or manifest.get("sample_sha256") != plan["sample_sha256"]
    ):
        raise TargetFinitePopulationError("target audit labels are not bound to the frozen pre-label plan")
    package = _required_mapping(manifest.get("annotation_package"), "annotation_package")
    required_names = {"contract", "ledger", "dataset_manifest", "resolution"}
    present_names = {
        field.removesuffix("_path")
        for field in package
        if isinstance(field, str) and field.endswith("_path")
    }
    if present_names != required_names and present_names != required_names | {"previous_ledger"}:
        raise TargetFinitePopulationError("target annotation package paths are incomplete")
    expected_fields = {f"{name}_{suffix}" for name in present_names for suffix in ("path", "sha256")}
    if set(package) != expected_fields:
        raise TargetFinitePopulationError("target annotation package descriptors are incomplete or contain extras")
    root = path.parent.resolve()
    paths = {
        name: _contained_package_file(
            root,
            root / _safe_relative_path(package[f"{name}_path"], f"annotation_package.{name}_path"),
            f"target annotation {name}",
        )
        for name in present_names
    }
    for name, package_path in paths.items():
        if _sha256_file(package_path) != _required_sha256(
            package.get(f"{name}_sha256"),
            f"annotation_package.{name}_sha256",
        ):
            raise TargetFinitePopulationError(f"target annotation {name} hash does not match its descriptor")
    expected_rows = _replay_target_annotation_package(plan, paths)
    if manifest.get("labels") != expected_rows:
        raise TargetFinitePopulationError("target audit labels do not replay from the annotation package")
    return expected_rows, manifest


def _replay_target_annotation_package(
    plan: Mapping[str, Any],
    paths: Mapping[str, Path],
) -> list[dict[str, Any]]:
    from football_tracking.candidate_annotations import (
        TARGET_AUDIT_USAGE,
        validate_candidate_annotation_package,
    )

    try:
        resolution = validate_candidate_annotation_package(
            paths["contract"],
            paths["ledger"],
            paths["dataset_manifest"],
            paths["resolution"],
            paths.get("previous_ledger"),
        )
    except (OSError, ValueError) as exc:
        raise TargetFinitePopulationError(f"target annotation package is invalid: {exc}") from exc
    header = _required_mapping(resolution.get("ledger_header"), "target annotation ledger header")
    if (
        header.get("usage") != TARGET_AUDIT_USAGE
        or header.get("qualification_scope") != TARGET_QUALIFICATION_SCOPE
        or header.get("target_run_id") != plan["bindings"]["target_run_id"]
        or header.get("target_audit_plan_sha256") != plan["plan_sha256"]
        or header.get("target_external_commitment_sha256")
        != plan["external_commitment"]["record_sha256"]
        or header.get("target_plan_commitment_sha256") != plan["plan_commitment_sha256"]
        or header.get("target_sampling_design_sha256") != plan["sampling_design_sha256"]
        or header.get("target_sample_sha256") != plan["sample_sha256"]
        or header.get("training_eligible") is not False
        or header.get("calibration_eligible") is not False
        or header.get("reusable") is not False
    ):
        raise TargetFinitePopulationError("target annotation ledger is not bound to the committed sample")
    if (
        resolution.get("artifact_type") != "target_finite_population_annotation_resolution"
        or resolution.get("qualification_scope") != TARGET_QUALIFICATION_SCOPE
        or resolution.get("training_eligible") is not False
        or resolution.get("reusable") is not False
    ):
        raise TargetFinitePopulationError("target annotation resolution envelope is invalid")
    contract = _load_json_file(paths["contract"], "target annotation contract")
    contract_candidates = _required_sequence(contract.get("candidates"), "target annotation contract candidates")
    contract_ids = [
        _required_text(row.get("candidate_id"), "target annotation contract candidate_id")
        for row in contract_candidates
        if isinstance(row, Mapping)
    ]
    sample_ids = [row["candidate_id"] for row in plan["sample"]]
    if sorted(contract_ids) != sorted(sample_ids) or len(contract_ids) != len(sample_ids):
        raise TargetFinitePopulationError("target annotation contract does not cover the exact committed sample")
    resolutions = {
        row["candidate_id"]: row
        for row in _required_sequence(resolution.get("resolutions"), "target annotation resolutions")
        if isinstance(row, Mapping)
    }
    vote_history = _required_sequence(resolution.get("vote_history"), "target annotation vote history")
    votes_by_candidate: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for index, vote in enumerate(vote_history):
        if not isinstance(vote, Mapping):
            raise TargetFinitePopulationError("target annotation vote history contains a non-object")
        candidate_id = _required_text(vote.get("candidate_id"), "target annotation vote candidate_id")
        votes_by_candidate.setdefault(candidate_id, []).append((index, vote))
    rows: list[dict[str, Any]] = []
    for sampled in plan["sample"]:
        candidate_id = sampled["candidate_id"]
        candidate_resolution = resolutions.get(candidate_id)
        votes = votes_by_candidate.get(candidate_id, [])
        primary = [(index, vote) for index, vote in votes if vote.get("stage") == "primary"]
        adjudication = [(index, vote) for index, vote in votes if vote.get("stage") == "adjudication"]
        if (
            candidate_resolution is None
            or candidate_resolution.get("status") != "confirmed"
            or candidate_resolution.get("training_eligible") is not False
            or len(primary) != 2
            or any(vote.get("reviewer_type") != "human" or vote.get("blind") is not True for _, vote in primary)
            or len({vote.get("annotator_id") for _, vote in primary}) != 2
            or len({vote.get("fingerprint") for _, vote in primary}) != 2
        ):
            raise TargetFinitePopulationError(
                f"target label {candidate_id!r} requires two distinct blind human primary reviewers"
            )
        resolution_source = candidate_resolution.get("resolution_source")
        if resolution_source == "human_adjudication":
            if (
                len(adjudication) != 1
                or adjudication[0][0] <= max(index for index, _ in primary)
                or adjudication[0][1].get("reviewer_type") != "human"
                or adjudication[0][1].get("annotator_id") in {vote.get("annotator_id") for _, vote in primary}
                or adjudication[0][1].get("fingerprint") in {vote.get("fingerprint") for _, vote in primary}
            ):
                raise TargetFinitePopulationError(
                    f"target label {candidate_id!r} requires ordered independent human adjudication"
                )
            primary_times = [_event_time(vote.get("created_at")) for _, vote in primary]
            if _event_time(adjudication[0][1].get("created_at")) <= max(primary_times):
                raise TargetFinitePopulationError("target adjudication must occur after both blind primary votes")
        elif resolution_source != "blind_primary_consensus" or adjudication:
            raise TargetFinitePopulationError(f"target label {candidate_id!r} has unresolved or unexpected votes")
        evidence_hashes = {
            _required_sha256(vote.get("evidence_sha256"), "target annotation vote evidence_sha256")
            for _, vote in [*primary, *adjudication]
        }
        if len(evidence_hashes) != 1:
            raise TargetFinitePopulationError("target annotation votes do not bind one exact evidence sample")
        label = candidate_resolution.get("label")
        if label != "match_ball" and label not in _NOISE_LABELS:
            raise TargetFinitePopulationError("target annotation resolution is not decisive")
        rows.append(
            {
                "candidate_id": candidate_id,
                "candidate_fingerprint": sampled["candidate_fingerprint"],
                "label": label,
                "evidence_sha256": next(iter(evidence_hashes)),
                "primary_vote_ids": [vote.get("vote_id") for _, vote in primary],
                "primary_reviewers": [
                    {
                        "annotator_id": vote.get("annotator_id"),
                        "fingerprint": vote.get("fingerprint"),
                    }
                    for _, vote in primary
                ],
                "adjudication_vote_ids": [vote.get("vote_id") for _, vote in adjudication],
                "resolution_source": resolution_source,
            }
        )
    return rows


def _event_time(value: Any) -> datetime:
    text = _required_text(value, "target annotation event timestamp")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TargetFinitePopulationError("target annotation event timestamp is invalid") from exc


def _normalized_population(population: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    rows = _required_sequence(population, "population")
    if not rows:
        raise TargetFinitePopulationError("target population must be non-empty")
    normalized: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_fingerprints: set[str] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise TargetFinitePopulationError(f"population[{index}] must be an object")
        disclosed = sorted(set(raw) & _TRUTH_FIELDS)
        if disclosed:
            raise TargetFinitePopulationError(f"target population discloses label or truth fields: {disclosed}")
        if set(raw) != {"candidate_id", "candidate_fingerprint"}:
            raise TargetFinitePopulationError("target population contains unsupported fields")
        candidate_id = _required_text(raw.get("candidate_id"), f"population[{index}].candidate_id")
        fingerprint = _required_sha256(
            raw.get("candidate_fingerprint"),
            f"population[{index}].candidate_fingerprint",
        )
        if candidate_id in seen_ids or fingerprint in seen_fingerprints:
            raise TargetFinitePopulationError("target population contains duplicate candidates or aliases")
        seen_ids.add(candidate_id)
        seen_fingerprints.add(fingerprint)
        normalized.append({"candidate_id": candidate_id, "candidate_fingerprint": fingerprint})
    return sorted(normalized, key=lambda row: row["candidate_id"])


def _normalized_decisions(
    decisions: Sequence[Mapping[str, Any]],
    population: Sequence[Mapping[str, str]],
) -> list[dict[str, Any]]:
    rows = _required_sequence(decisions, "frozen decisions")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    population_by_id = {row["candidate_id"]: row for row in population}
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping) or set(raw) != _DECISION_ROW_FIELDS:
            raise TargetFinitePopulationError("frozen decision row fields are invalid")
        candidate_id = _required_text(raw.get("candidate_id"), f"decisions[{index}].candidate_id")
        if candidate_id in seen:
            raise TargetFinitePopulationError("frozen decisions contain duplicate candidates")
        seen.add(candidate_id)
        population_row = population_by_id.get(candidate_id)
        decision = raw.get("decision")
        if (
            population_row is None
            or raw.get("candidate_fingerprint") != population_row["candidate_fingerprint"]
            or decision not in _DECISION_VALUES
        ):
            raise TargetFinitePopulationError("frozen decision does not match the exact target population")
        _validate_full_decision_row(raw, candidate_id=candidate_id, index=index)
        normalized.append(dict(raw))
    if seen != set(population_by_id):
        raise TargetFinitePopulationError("frozen decisions do not cover the exact target population")
    return sorted(normalized, key=lambda row: row["candidate_id"])


def _validate_full_decision_row(
    row: Mapping[str, Any],
    *,
    candidate_id: str,
    index: int,
) -> None:
    _required_text(row.get("variant_id"), f"decisions[{index}].variant_id")
    frame_index = row.get("frame_index")
    if isinstance(frame_index, bool) or not isinstance(frame_index, int) or frame_index < 0:
        raise TargetFinitePopulationError(f"decisions[{index}].frame_index is invalid")
    accept_score = _decision_probability(row.get("accept_score"), candidate_id, "accept_score")
    reject_score = _decision_probability(row.get("reject_score"), candidate_id, "reject_score")
    unknown_score = _decision_probability(row.get("unknown_score"), candidate_id, "unknown_score")
    if not math.isclose(accept_score + reject_score + unknown_score, 1.0, abs_tol=1e-6):
        raise TargetFinitePopulationError(f"frozen decision {candidate_id} lane scores do not sum to one")
    top_label = row.get("top_label")
    if top_label not in {*_NOISE_LABELS, "match_ball", "unknown"}:
        raise TargetFinitePopulationError(f"frozen decision {candidate_id} top_label is invalid")
    _decision_probability(row.get("top_margin"), candidate_id, "top_margin")
    if top_label == "match_ball" and accept_score < unknown_score:
        raise TargetFinitePopulationError(f"frozen decision {candidate_id} top_label contradicts its scores")
    if top_label == "unknown" and unknown_score <= accept_score:
        raise TargetFinitePopulationError(f"frozen decision {candidate_id} top_label contradicts its scores")
    if top_label in _NOISE_LABELS and reject_score < max(accept_score, unknown_score):
        raise TargetFinitePopulationError(f"frozen decision {candidate_id} top_label contradicts its scores")

    raw_decision = row.get("raw_decision")
    decision = row.get("decision")
    if raw_decision not in _DECISION_VALUES or decision not in _DECISION_VALUES:
        raise TargetFinitePopulationError(f"frozen decision {candidate_id} value is invalid")
    scope = row.get("decision_scope")
    role = row.get("policy_role")
    if scope not in {"application", "evaluation_only"}:
        raise TargetFinitePopulationError(f"frozen decision {candidate_id} scope is invalid")
    if role not in {None, "policy_calibration", "policy_audit"}:
        raise TargetFinitePopulationError(f"frozen decision {candidate_id} policy role is invalid")
    if (scope == "evaluation_only") != (role is not None):
        raise TargetFinitePopulationError(f"frozen decision {candidate_id} scope and policy role disagree")
    reasons = row.get("forced_abstain_reasons")
    if (
        not isinstance(reasons, list)
        or reasons != sorted(set(reasons))
        or not all(isinstance(reason, str) and reason in _DECISION_FORCED_REASONS for reason in reasons)
    ):
        raise TargetFinitePopulationError(f"frozen decision {candidate_id} reasons are invalid")
    if scope == "evaluation_only" and (
        decision != "abstain"
        or raw_decision != "abstain"
        or "evaluation_holdout" not in reasons
    ):
        raise TargetFinitePopulationError(f"frozen decision {candidate_id} evaluation holdout is invalid")
    if not reasons and decision != raw_decision:
        raise TargetFinitePopulationError(f"frozen decision {candidate_id} does not match its raw decision")
    preserved = row.get("existing_decision_preserved")
    applied = row.get("applied_to_contract")
    if not isinstance(preserved, bool) or not isinstance(applied, bool):
        raise TargetFinitePopulationError(f"frozen decision {candidate_id} application flags are invalid")
    expected_applied = scope == "application" and decision in {"accept", "reject"} and not preserved
    if applied is not expected_applied:
        raise TargetFinitePopulationError(f"frozen decision {candidate_id} applied flag is invalid")


def _decision_probability(value: Any, candidate_id: str, field: str) -> float:
    if isinstance(value, bool):
        raise TargetFinitePopulationError(f"frozen decision {candidate_id} {field} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TargetFinitePopulationError(
            f"frozen decision {candidate_id} {field} must be finite"
        ) from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise TargetFinitePopulationError(
            f"frozen decision {candidate_id} {field} must be between zero and one"
        )
    return number


def _validated_frozen_application(application: Mapping[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(application, Mapping)
        or application.get("schema_version") != "1.0"
        or application.get("artifact_type") != "target_finite_population_application"
        or application.get("qualification_scope") != TARGET_QUALIFICATION_SCOPE
        or application.get("status") != "frozen_before_labels"
        or application.get("training_eligible") is not False
        or application.get("reusable") is not False
        or application.get("promotion_scope") != "exact_target_only"
    ):
        raise TargetFinitePopulationError("target frozen application envelope is invalid")
    content_sha256 = _required_sha256(
        application.get("application_content_sha256"),
        "frozen application content sha256",
    )
    content = {
        key: value for key, value in application.items() if key not in {"generated_at", "application_content_sha256"}
    }
    if content_sha256 != _canonical_sha256(content):
        raise TargetFinitePopulationError("target frozen application content hash is invalid")
    raw_decisions = _required_sequence(application.get("decisions"), "frozen application decisions")
    for row in raw_decisions:
        if isinstance(row, Mapping):
            disclosed = sorted(set(row) & _TRUTH_FIELDS)
            if disclosed:
                raise TargetFinitePopulationError(
                    f"target frozen application discloses label or truth fields: {disclosed}"
                )
    population = [
        {
            "candidate_id": row.get("candidate_id"),
            "candidate_fingerprint": row.get("candidate_fingerprint"),
        }
        for row in raw_decisions
        if isinstance(row, Mapping)
    ]
    normalized_population = _normalized_population(population)
    decisions = _normalized_decisions(raw_decisions, normalized_population)
    evidence = _required_mapping(application.get("target_binding_evidence"), "target binding evidence")
    expected_evidence = {
        "source_sha256": _required_sha256(evidence.get("source_sha256"), "source_sha256"),
        "root_contract_sha256": _required_sha256(
            evidence.get("root_contract_sha256"),
            "root_contract_sha256",
        ),
        "candidate_population_sha256": _canonical_sha256(normalized_population),
        "model_sha256": _required_sha256(evidence.get("model_sha256"), "model_sha256"),
        "model_version": _required_text(evidence.get("model_version"), "model_version"),
        "policy_sha256": _required_sha256(evidence.get("policy_sha256"), "policy_sha256"),
        "policy_version": _required_text(evidence.get("policy_version"), "policy_version"),
        "thresholds_sha256": _required_sha256(
            evidence.get("thresholds_sha256"),
            "thresholds_sha256",
        ),
    }
    if evidence != expected_evidence:
        raise TargetFinitePopulationError("target frozen application binding evidence is invalid")
    if (
        application.get("model_version") != expected_evidence["model_version"]
        or application.get("policy_version") != expected_evidence["policy_version"]
    ):
        raise TargetFinitePopulationError("target frozen application version binding is invalid")
    return {
        **dict(application),
        "decisions": decisions,
        "target_binding_evidence": expected_evidence,
    }


def _ranked_sample(
    population: Sequence[Mapping[str, str]],
    decisions: Sequence[Mapping[str, Any]],
    *,
    sample_size: int,
    ordering_salt: str,
) -> list[dict[str, Any]]:
    return _ranked_order(
        population,
        decisions,
        ordering_salt=ordering_salt,
    )[:sample_size]


def _ranked_order(
    population: Sequence[Mapping[str, str]],
    decisions: Sequence[Mapping[str, Any]],
    *,
    ordering_salt: str,
) -> list[dict[str, Any]]:
    decisions_by_id = {row["candidate_id"]: row for row in decisions}
    ordered = sorted(
        population,
        key=lambda row: (
            _canonical_sha256(
                {
                    "algorithm": SAMPLING_ALGORITHM,
                    "ordering_salt": ordering_salt,
                    "candidate_id": row["candidate_id"],
                    "candidate_fingerprint": row["candidate_fingerprint"],
                }
            ),
            row["candidate_id"],
        ),
    )
    return [
        {
            "order": index,
            **row,
            "decision": decisions_by_id[row["candidate_id"]]["decision"],
        }
        for index, row in enumerate(ordered)
    ]


def _validated_bindings(bindings: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(bindings, Mapping) or set(bindings) != _BINDING_FIELDS:
        raise TargetFinitePopulationError("target qualification bindings are incomplete or contain extra fields")
    normalized: dict[str, str] = {}
    for field in sorted(_BINDING_FIELDS):
        value = bindings.get(field)
        normalized[field] = (
            _required_sha256(value, field) if field in _HASH_BINDING_FIELDS else _required_text(value, field)
        )
    return normalized


def _target_commitment_identity(
    *,
    application_content_sha256: str,
    bindings: Mapping[str, str],
    population_sha256: str,
) -> dict[str, Any]:
    return {
        "frozen_application_content_sha256": _required_sha256(
            application_content_sha256,
            "frozen_application_content_sha256",
        ),
        "bindings": _validated_bindings(bindings),
        "population_sha256": _required_sha256(population_sha256, "population_sha256"),
    }


def _sampling_size_rule(
    *,
    population_size: int,
    sample_size: int,
) -> dict[str, Any]:
    return {
        "algorithm": SAMPLING_SIZE_RULE,
        "true_ball_support_minimum": MIN_TRUE_BALL_SUPPORT,
        "true_ball_prevalence_lower_bound": SAMPLING_TRUE_BALL_PREVALENCE_LOWER_BOUND,
        "required_draw_count": SAMPLING_REQUIRED_DRAW_COUNT,
        "population_size": population_size,
        "derived_sample_size": sample_size,
    }


def _publish_exclusive_commitment_record(
    commitment_root: Path,
    *,
    record_name: str,
    record_bytes: bytes,
) -> Path:
    root = Path(os.path.abspath(commitment_root))
    final_name = _safe_record_name(record_name)
    if len(record_bytes) > MAX_TARGET_COMMITMENT_BYTES:
        raise TargetFinitePopulationError("target pre-label commitment exceeds the size limit")
    staging_name = f".{final_name}.{secrets.token_hex(16)}.staging"
    with _opened_registry_directory(root, create=True) as (registry_root, registry_fd):
        descriptor = _create_registry_staging_file(
            registry_root,
            registry_fd,
            staging_name,
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise TargetFinitePopulationError(
                    "target pre-label commitment staging entry must be a regular file"
                )
            offset = 0
            while offset < len(record_bytes):
                written = os.write(descriptor, record_bytes[offset:])
                if written <= 0:
                    raise OSError("short write")
                offset += written
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            _remove_registry_entry(registry_root, registry_fd, staging_name)
            raise
        else:
            os.close(descriptor)

        try:
            _publish_registry_entry_no_replace(
                registry_root,
                registry_fd,
                staging_name,
                final_name,
            )
        except FileExistsError:
            if (
                _capture_registry_entry(
                    registry_root,
                    registry_fd,
                    final_name,
                    "target pre-label commitment",
                )
                != record_bytes
            ):
                raise TargetFinitePopulationError(
                    "target already has a different pre-label commitment"
                )
        finally:
            _remove_registry_entry(registry_root, registry_fd, staging_name)
            _fsync_registry_directory(registry_fd)
    return root / final_name


def _validate_external_commitment_bytes(
    plan: Mapping[str, Any],
    record_bytes: bytes,
    *,
    record_name: str,
) -> None:
    descriptor = _required_mapping(plan.get("external_commitment"), "external_commitment")
    if set(descriptor) != {
        "artifact_type",
        "target_key",
        "record_name",
        "record_sha256",
    }:
        raise TargetFinitePopulationError(
            "target external commitment descriptor is incomplete or contains extras"
        )
    if descriptor.get("artifact_type") != TARGET_PRELABEL_COMMITMENT_TYPE:
        raise TargetFinitePopulationError("target external commitment artifact type is invalid")
    target_key = _required_sha256(descriptor.get("target_key"), "external_commitment.target_key")
    record_sha256 = _required_sha256(
        descriptor.get("record_sha256"),
        "external_commitment.record_sha256",
    )
    expected_record_name = _safe_record_name(descriptor.get("record_name"))
    if _safe_record_name(record_name) != expected_record_name:
        raise TargetFinitePopulationError("target external commitment record name does not match")
    if hashlib.sha256(record_bytes).hexdigest() != record_sha256:
        raise TargetFinitePopulationError("target external commitment record hash is invalid")
    try:
        record = json.loads(record_bytes.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise TargetFinitePopulationError("target external commitment record is invalid") from exc
    if not isinstance(record, dict) or _json_bytes(record) != record_bytes:
        raise TargetFinitePopulationError("target external commitment record is not canonical")
    identity = _target_commitment_identity(
        application_content_sha256=plan["frozen_application_content_sha256"],
        bindings=plan["bindings"],
        population_sha256=plan["bindings"]["candidate_population_sha256"],
    )
    expected_target_key = _canonical_sha256(identity)
    design = _required_mapping(plan.get("sampling_design"), "sampling_design")
    expected_record = {
        "schema_version": TARGET_AUDIT_SCHEMA_VERSION,
        "artifact_type": TARGET_PRELABEL_COMMITMENT_TYPE,
        "qualification_scope": TARGET_QUALIFICATION_SCOPE,
        "status": "committed_before_labels",
        "algorithm": EXTERNAL_COMMITMENT_ALGORITHM,
        "target_key": expected_target_key,
        "target_identity": identity,
        "plan_sha256": plan["plan_sha256"],
        "plan_commitment_sha256": plan["plan_commitment_sha256"],
        "ordering_salt": design["ordering_salt"],
        "sample_size_rule": design["sample_size_rule"],
        "sampling_design_sha256": plan["sampling_design_sha256"],
        "sample_sha256": plan["sample_sha256"],
    }
    if target_key != expected_target_key or record != expected_record:
        raise TargetFinitePopulationError(
            "target external commitment does not match the exact pre-label plan"
        )


def _safe_record_name(value: Any) -> str:
    name = _required_text(value, "external commitment record_name")
    if Path(name).name != name or name in {".", ".."}:
        raise TargetFinitePopulationError("external commitment record_name is unsafe")
    return name


def _read_regular_file_bytes(path: Path, label: str) -> bytes:
    target = Path(os.path.abspath(path))
    with _opened_registry_directory(target.parent, create=False) as (registry_root, registry_fd):
        return _capture_registry_entry(
            registry_root,
            registry_fd,
            _safe_record_name(target.name),
            label,
        )


def capture_target_prelabel_registry(
    commitment_root: Path,
    *,
    record_name: str,
) -> tuple[bytes, list[tuple[str, bytes]]]:
    """Capture the canonical record and registry entries from one stable directory handle."""

    expected_name = _safe_record_name(record_name)
    with _opened_registry_directory(commitment_root, create=False) as (
        registry_root,
        registry_fd,
    ):
        names = _registry_entry_names(registry_root, registry_fd)
        if expected_name not in names:
            raise FileNotFoundError(expected_name)
        records: list[tuple[str, bytes]] = []
        for name in names:
            if _STAGING_RECORD.fullmatch(name):
                continue
            captured = _capture_registry_entry(
                registry_root,
                registry_fd,
                name,
                "canonical pre-label commitment registry entry",
            )
            records.append((name, captured))
        return dict(records)[expected_name], records


def capture_target_prelabel_commitment_file(path: Path) -> bytes:
    """Capture one bundled commitment through stable parent and entry handles."""

    return _read_regular_file_bytes(path, "bundled target pre-label commitment")


@contextmanager
def _opened_registry_directory(
    path: Path,
    *,
    create: bool,
) -> Iterator[tuple[Path, int | _WindowsDirectoryChain | None]]:
    root = Path(os.path.abspath(path))
    if os.name == "nt":
        chain = _open_windows_directory_chain(root, create=create)
        try:
            _assert_windows_directory_chain_identity(chain)
            yield root, chain
            _assert_windows_directory_chain_identity(chain)
        finally:
            _close_windows_handles(chain)
        return

    descriptor = _open_posix_directory_chain(root, create=create)
    try:
        yield root, descriptor
        _assert_registry_path_identity(root, descriptor)
    finally:
        os.close(descriptor)


def _open_posix_directory_chain(path: Path, *, create: bool) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _assert_registry_path_identity(path: Path, descriptor: int) -> None:
    try:
        path_metadata = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise TargetFinitePopulationError(
            "target pre-label commitment registry path changed while in use"
        ) from exc
    handle_metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(path_metadata.st_mode)
        or path_metadata.st_dev != handle_metadata.st_dev
        or path_metadata.st_ino != handle_metadata.st_ino
    ):
        raise TargetFinitePopulationError(
            "target pre-label commitment registry path changed while in use"
        )


def _open_windows_directory_chain(path: Path, *, create: bool) -> _WindowsDirectoryChain:
    if not path.anchor:
        raise TargetFinitePopulationError(
            "target pre-label commitment registry must be absolute"
        )
    components: list[_WindowsDirectoryComponent] = []
    current = Path(path.anchor)
    try:
        for index, part in enumerate((path.anchor, *path.parts[1:])):
            if index:
                current /= part
                if create:
                    if components:
                        _assert_windows_directory_chain_identity(
                            _WindowsDirectoryChain(tuple(components))
                        )
                    try:
                        os.mkdir(current)
                    except FileExistsError:
                        pass
                    if components:
                        _assert_windows_directory_chain_identity(
                            _WindowsDirectoryChain(tuple(components))
                        )
            handle = _open_windows_path_handle(
                current,
                desired_access=(
                    _WINDOWS_FILE_LIST_DIRECTORY
                    | _WINDOWS_FILE_READ_ATTRIBUTES
                    | _WINDOWS_SYNCHRONIZE
                ),
                flags=(
                    _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS
                    | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT
                ),
            )
            attributes, identity = _windows_handle_information(handle)
            component = _WindowsDirectoryComponent(
                path=current,
                handle=handle,
                identity=identity,
            )
            components.append(component)
            if (
                attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
                or not attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY
            ):
                raise TargetFinitePopulationError(
                    "target pre-label commitment registry must contain only non-link directories"
                )
            if _windows_path_identity(current, directory=True) != identity:
                raise TargetFinitePopulationError(
                    "target pre-label commitment registry path changed while in use"
                )
        return _WindowsDirectoryChain(tuple(components))
    except BaseException:
        _close_windows_handles(_WindowsDirectoryChain(tuple(components)))
        raise


def _open_windows_path_handle(
    path: Path,
    *,
    desired_access: int,
    flags: int,
) -> int:
    import ctypes
    from ctypes import wintypes

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        desired_access,
        _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE,
        None,
        _WINDOWS_OPEN_EXISTING,
        flags,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError()
    return int(handle)


def _windows_handle_information(handle: int) -> tuple[int, tuple[int, int]]:
    import ctypes
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    get_information = ctypes.windll.kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    get_information.restype = wintypes.BOOL
    information = _ByHandleFileInformation()
    if not get_information(handle, ctypes.byref(information)):
        raise ctypes.WinError()
    file_index = (information.nFileIndexHigh << 32) | information.nFileIndexLow
    return (
        int(information.dwFileAttributes),
        (int(information.dwVolumeSerialNumber), int(file_index)),
    )


def _windows_path_identity(path: Path, *, directory: bool) -> tuple[int, int]:
    import ctypes

    handle = _open_windows_path_handle(
        path,
        desired_access=(
            (
                _WINDOWS_FILE_LIST_DIRECTORY
                if directory
                else _WINDOWS_GENERIC_READ
            )
            | _WINDOWS_FILE_READ_ATTRIBUTES
            | _WINDOWS_SYNCHRONIZE
        ),
        flags=(
            _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS
            | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT
            | (0 if directory else _WINDOWS_FILE_FLAG_SEQUENTIAL_SCAN)
        ),
    )
    try:
        attributes, identity = _windows_handle_information(handle)
        is_directory = bool(attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY)
        if (
            attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
            or is_directory != directory
        ):
            raise TargetFinitePopulationError(
                "target pre-label commitment registry path identity is unsafe"
            )
        return identity
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _assert_windows_directory_chain_identity(
    chain: _WindowsDirectoryChain,
) -> None:
    try:
        for component in chain.components:
            _attributes, handle_identity = _windows_handle_information(
                component.handle
            )
            if (
                handle_identity != component.identity
                or _windows_path_identity(component.path, directory=True)
                != component.identity
            ):
                raise TargetFinitePopulationError(
                    "target pre-label commitment registry path changed while in use"
                )
    except OSError as exc:
        raise TargetFinitePopulationError(
            "target pre-label commitment registry path changed while in use"
        ) from exc


def _close_windows_handles(
    handles: _WindowsDirectoryChain | Sequence[int],
) -> None:
    raw_handles = (
        [component.handle for component in handles.components]
        if isinstance(handles, _WindowsDirectoryChain)
        else list(handles)
    )
    if not raw_handles:
        return
    import ctypes

    for handle in reversed(raw_handles):
        ctypes.windll.kernel32.CloseHandle(handle)


@contextmanager
def _guard_windows_registry_operation(
    registry_handle: int | _WindowsDirectoryChain | None,
) -> Iterator[None]:
    if not isinstance(registry_handle, _WindowsDirectoryChain):
        yield
        return
    _assert_windows_directory_chain_identity(registry_handle)
    try:
        yield
    finally:
        _assert_windows_directory_chain_identity(registry_handle)


def _create_registry_staging_file(
    root: Path,
    registry_fd: int | _WindowsDirectoryChain | None,
    staging_name: str,
) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    with _guard_windows_registry_operation(registry_fd):
        if isinstance(registry_fd, int):
            return os.open(staging_name, flags, 0o600, dir_fd=registry_fd)
        return os.open(root / staging_name, flags, 0o600)


def _publish_registry_entry_no_replace(
    root: Path,
    registry_fd: int | _WindowsDirectoryChain | None,
    staging_name: str,
    final_name: str,
) -> None:
    with _guard_windows_registry_operation(registry_fd):
        if not isinstance(registry_fd, int):
            _publish_windows_registry_entry_no_replace(
                root / staging_name,
                root / final_name,
            )
            return
        os.link(
            staging_name,
            final_name,
            src_dir_fd=registry_fd,
            dst_dir_fd=registry_fd,
            follow_symlinks=False,
        )
    _fsync_registry_directory(registry_fd)


def _publish_windows_registry_entry_no_replace(source: Path, target: Path) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file = kernel32.MoveFileExW
    move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file.restype = wintypes.BOOL
    if move_file(str(source), str(target), 0x00000008):
        return
    error = ctypes.get_last_error()
    if error in {80, 183}:
        raise FileExistsError(error, "target pre-label commitment already exists", str(target))
    raise ctypes.WinError(error)


def _remove_registry_entry(
    root: Path,
    registry_fd: int | _WindowsDirectoryChain | None,
    name: str,
) -> None:
    with _guard_windows_registry_operation(registry_fd):
        try:
            if not isinstance(registry_fd, int):
                os.unlink(root / name)
                return
            os.unlink(name, dir_fd=registry_fd)
        except FileNotFoundError:
            pass


def _fsync_registry_directory(
    registry_fd: int | _WindowsDirectoryChain | None,
) -> None:
    if isinstance(registry_fd, int):
        os.fsync(registry_fd)
    elif isinstance(registry_fd, _WindowsDirectoryChain):
        _assert_windows_directory_chain_identity(registry_fd)


def _registry_entry_names(
    root: Path,
    registry_fd: int | _WindowsDirectoryChain | None,
) -> list[str]:
    with _guard_windows_registry_operation(registry_fd):
        try:
            names = os.listdir(
                registry_fd if isinstance(registry_fd, int) else root
            )
        except OSError as exc:
            raise TargetFinitePopulationError(
                f"canonical target pre-label commitment registry is unavailable: {exc}"
            ) from exc
    return sorted(_safe_record_name(name) for name in names)


def _capture_registry_entry(
    root: Path,
    registry_fd: int | _WindowsDirectoryChain | None,
    name: str,
    label: str,
) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    with _guard_windows_registry_operation(registry_fd):
        try:
            descriptor = (
                os.open(name, flags, dir_fd=registry_fd)
                if isinstance(registry_fd, int)
                else _open_windows_registry_entry(root / name)
            )
        except OSError as exc:
            raise TargetFinitePopulationError(f"{label} is unavailable: {exc}") from exc
        try:
            before = os.fstat(descriptor)
            windows_identity_before = (
                _windows_descriptor_identity(descriptor)
                if isinstance(registry_fd, _WindowsDirectoryChain)
                else None
            )
            if (
                windows_identity_before is not None
                and _windows_path_identity(root / name, directory=False)
                != windows_identity_before
            ):
                raise TargetFinitePopulationError(
                    f"{label} changed while it was captured"
                )
            if not stat.S_ISREG(before.st_mode):
                raise TargetFinitePopulationError(f"{label} must be a regular file")
            if before.st_size > MAX_TARGET_COMMITMENT_BYTES:
                raise TargetFinitePopulationError(f"{label} exceeds the size limit")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(64 * 1024, MAX_TARGET_COMMITMENT_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_TARGET_COMMITMENT_BYTES:
                    raise TargetFinitePopulationError(f"{label} exceeds the size limit")
            after = os.fstat(descriptor)
            windows_identity_after = (
                _windows_descriptor_identity(descriptor)
                if isinstance(registry_fd, _WindowsDirectoryChain)
                else None
            )
            try:
                path_after = (
                    os.stat(name, dir_fd=registry_fd, follow_symlinks=False)
                    if isinstance(registry_fd, int)
                    else os.stat(root / name, follow_symlinks=False)
                )
            except OSError as exc:
                raise TargetFinitePopulationError(
                    f"{label} changed while it was captured"
                ) from exc
            identity_before = (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            identity_after = (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            payload = b"".join(chunks)
            if (
                identity_before != identity_after
                or len(payload) != after.st_size
                or path_after.st_dev != after.st_dev
                or path_after.st_ino != after.st_ino
                or path_after.st_mode != after.st_mode
                or path_after.st_size != after.st_size
                or windows_identity_before != windows_identity_after
                or (
                    windows_identity_after is not None
                    and _windows_path_identity(root / name, directory=False)
                    != windows_identity_after
                )
            ):
                raise TargetFinitePopulationError(f"{label} changed while it was captured")
            return payload
        finally:
            os.close(descriptor)


def _open_windows_registry_entry(
    path: Path,
    *,
    label: str = "target pre-label commitment",
) -> int:
    import ctypes
    import msvcrt

    handle = _open_windows_path_handle(
        path,
        desired_access=(
            _WINDOWS_GENERIC_READ
            | _WINDOWS_FILE_READ_ATTRIBUTES
            | _WINDOWS_SYNCHRONIZE
        ),
        flags=(
            _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS
            | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT
            | _WINDOWS_FILE_FLAG_SEQUENTIAL_SCAN
        ),
    )
    try:
        attributes, _identity = _windows_handle_information(handle)
        if (
            attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
            or attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY
        ):
            raise TargetFinitePopulationError(
                f"{label} must be a regular non-link file"
            )
        return msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
    except BaseException:
        ctypes.windll.kernel32.CloseHandle(handle)
        raise


def _windows_descriptor_identity(descriptor: int) -> tuple[int, int]:
    import msvcrt

    _attributes, identity = _windows_handle_information(
        msvcrt.get_osfhandle(descriptor)
    )
    return identity


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _hypergeometric_lower_tail(
    errors: int,
    *,
    population_size: int,
    population_errors: int,
    sample_size: int,
) -> float:
    minimum = max(0, sample_size - (population_size - population_errors))
    maximum = min(errors, sample_size, population_errors)
    if minimum > maximum:
        return 0.0
    denominator = _log_combination(population_size, sample_size)
    terms = [
        _log_combination(population_errors, observed)
        + _log_combination(population_size - population_errors, sample_size - observed)
        - denominator
        for observed in range(minimum, maximum + 1)
    ]
    anchor = max(terms)
    return min(1.0, math.exp(anchor) * sum(math.exp(term - anchor) for term in terms))


def _binomial_lower_tail(errors: int, total: int, probability: float) -> float:
    if probability <= 0.0:
        return 1.0
    if probability >= 1.0:
        return 1.0 if errors >= total else 0.0
    term = (1.0 - probability) ** total
    total_probability = term
    ratio = probability / (1.0 - probability)
    for observed in range(errors):
        term *= (total - observed) / (observed + 1) * ratio
        total_probability += term
    return min(1.0, max(0.0, total_probability))


def _log_combination(n: int, k: int) -> float:
    if k < 0 or k > n:
        return float("-inf")
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def _validate_count_pair(errors: int, total: int) -> None:
    if (
        isinstance(errors, bool)
        or not isinstance(errors, int)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total < 0
        or not 0 <= errors <= total
    ):
        raise TargetFinitePopulationError("invalid error and total counts")


def _validate_alpha(alpha: float) -> None:
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not 0.0 < float(alpha) < 0.5:
        raise TargetFinitePopulationError("alpha must be between zero and one half")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _required_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TargetFinitePopulationError(f"{label} must be an object")
    return value


def _required_sequence(value: Any, label: str) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TargetFinitePopulationError(f"{label} must be a list")
    return list(value)


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TargetFinitePopulationError(f"{label} must be non-empty text")
    return value.strip()


def _required_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise TargetFinitePopulationError(f"{label} must be a lowercase SHA-256")
    return value


def _load_json_file(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetFinitePopulationError(f"{label} is invalid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise TargetFinitePopulationError(f"{label} must be an object")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _safe_relative_path(value: Any, label: str) -> Path:
    text = _required_text(value, label)
    relative = Path(text)
    if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
        raise TargetFinitePopulationError(f"{label} must be a contained relative path")
    return relative


def _contained_package_file(root: Path, path: Path, label: str) -> Path:
    candidate = Path(path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise TargetFinitePopulationError(f"{label} escapes the annotation package root") from exc
    if not candidate.is_file():
        raise TargetFinitePopulationError(f"{label} is unavailable")
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
