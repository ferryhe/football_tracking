from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from string import hexdigits
from typing import Any

from football_tracking.tracking_contracts import (
    CLASSIFICATION_LABELS,
    CONFIRMED_LABEL_ORIGINS,
    TRACKING_CONTRACT_REPORT_NAME,
    build_tracking_contract,
)
from football_tracking.tracking_contracts import (
    SCHEMA_VERSION as TRACKING_CONTRACT_SCHEMA_VERSION,
)

LEDGER_SCHEMA_VERSION = "1.0"
ANNOTATION_SCHEMA_VERSION = "1.0"
ANNOTATION_RESOLUTION_NAME = "annotation_resolution.v1.json"
ADJUDICATION_QUEUE_NAME = "annotation_adjudication_queue.v1.json"
DATASET_MANIFEST_NAME = "candidate_dataset_manifest.json"
EVIDENCE_HASH_POLICY = "candidate-tight-context-montage-hashes-v1"

_VOTE_STAGES = frozenset({"primary", "adjudication"})
_REVIEWER_TYPES = frozenset({"ai", "human"})
_EVIDENCE_ARTIFACTS = ("tight_tensor", "context_tensor", "review_montage")
_ANNOTATION_ARTIFACT_ORDER = (
    ANNOTATION_RESOLUTION_NAME,
    ADJUDICATION_QUEUE_NAME,
    TRACKING_CONTRACT_REPORT_NAME,
)


@dataclass(frozen=True)
class _FileSnapshot:
    path: Path
    sha256: str
    label: str


def sample_evidence_sha256(sample: dict[str, Any]) -> str:
    """Hash the exact tight/context/montage artifact hashes bound to one vote."""

    if not isinstance(sample, dict):
        raise ValueError("dataset sample must be an object")
    candidate_id = _plain_required_text(sample.get("candidate_id"), "sample.candidate_id")
    sample_id = _plain_required_text(sample.get("sample_id"), "sample.sample_id")
    artifacts = sample.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError(f"dataset sample {sample_id!r} artifacts must be an object")
    artifact_hashes: dict[str, str] = {}
    for name in _EVIDENCE_ARTIFACTS:
        descriptor = artifacts.get(name)
        if not isinstance(descriptor, dict):
            raise ValueError(f"dataset sample {sample_id!r} missing {name!r} artifact")
        artifact_hashes[name] = _plain_sha256_text(descriptor.get("sha256"), f"sample.{name}.sha256")
    payload = {
        "policy": EVIDENCE_HASH_POLICY,
        "candidate_id": candidate_id,
        "sample_id": sample_id,
        "artifacts": artifact_hashes,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_candidate_annotations(
    contract_path: Path,
    ledger_path: Path,
    output_dir: Path,
    *,
    min_confidence: float = 0.8,
    dataset_manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Resolve blind votes without projecting audit metadata into the V2 contract."""

    contract_path = Path(contract_path)
    ledger_path = Path(ledger_path)
    output_dir = Path(output_dir)
    if dataset_manifest_path is None:
        discovered_manifest = ledger_path.parent / DATASET_MANIFEST_NAME
        dataset_manifest_path = discovered_manifest if discovered_manifest.is_file() else None
    else:
        dataset_manifest_path = Path(dataset_manifest_path)
    threshold = _confidence_threshold(min_confidence)
    source_contract_path = _contract_file_path(contract_path)
    final_paths = tuple(
        output_dir / name
        for name in (ANNOTATION_RESOLUTION_NAME, ADJUDICATION_QUEUE_NAME, TRACKING_CONTRACT_REPORT_NAME)
    )
    _reject_input_output_aliases(
        source_contract_path=source_contract_path,
        ledger_path=ledger_path,
        dataset_manifest_path=dataset_manifest_path,
        final_paths=final_paths,
    )

    contract, contract_snapshot = _load_tracking_contract_snapshot(source_contract_path)
    candidates = contract.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("tracking contract must contain at least one candidate")
    candidate_ids = [str(candidate["candidate_id"]) for candidate in candidates]
    candidate_id_set = set(candidate_ids)

    contract_sha256 = contract_snapshot.sha256
    ledger_header, raw_votes, votes, ledger_snapshot = load_vote_ledger(
        ledger_path,
        candidate_ids=candidate_id_set,
        contract_sha256=contract_sha256,
    )
    dataset_binding = _validate_dataset_evidence(
        dataset_manifest_path,
        ledger_header=ledger_header,
        votes=votes,
        candidate_ids=candidate_id_set,
        contract_sha256=contract_sha256,
    )
    votes_by_candidate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for vote in votes:
        votes_by_candidate[vote["candidate_id"]].append(vote)

    classifications = [dict(row) for row in contract["classifications"]]
    confirmed_by_candidate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for classification in classifications:
        if classification.get("label_origin") in CONFIRMED_LABEL_ORIGINS:
            confirmed_by_candidate[str(classification["candidate_id"])].append(classification)

    resolutions: list[dict[str, Any]] = []
    adjudication_queue: list[dict[str, Any]] = []
    additions: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        existing_confirmed = confirmed_by_candidate.get(candidate_id, [])
        if existing_confirmed:
            resolution = _resolve_existing_confirmed(candidate_id, existing_confirmed)
        else:
            resolution = _resolve_votes(
                candidate_id,
                votes_by_candidate.get(candidate_id, []),
                min_confidence=threshold,
            )
            if resolution["status"] == "confirmed":
                additions.append(
                    {
                        "candidate_id": candidate_id,
                        "label": resolution["label"],
                        "label_origin": resolution["label_origin"],
                        "confidence": resolution["confidence"],
                    }
                )
            elif not _has_unknown_classification(classifications, candidate_id):
                additions.append(
                    {
                        "candidate_id": candidate_id,
                        "label": "unknown",
                        "label_origin": "prelabel",
                        "confidence": 0.0,
                    }
                )
        resolutions.append(resolution)
        if resolution["status"] != "confirmed":
            adjudication_queue.append(
                {
                    "candidate_id": candidate_id,
                    "status": "pending_adjudication",
                    "reasons": list(resolution["reasons"]),
                    "primary_vote_ids": list(resolution["primary_vote_ids"]),
                    "adjudication_vote_ids": list(resolution["adjudication_vote_ids"]),
                }
            )

    derived_contract = build_tracking_contract(
        source=contract.get("source"),
        frames=contract["frames"],
        candidates=contract["candidates"],
        classifications=[*classifications, *additions],
        decisions=contract["decisions"],
    )
    if derived_contract["validation_errors"]:
        raise ValueError(f"derived tracking contract is invalid: {derived_contract['validation_errors']}")
    derived_contract_bytes = _json_bytes(derived_contract)
    derived_contract_sha256 = hashlib.sha256(derived_contract_bytes).hexdigest()

    report = {
        "schema_version": ANNOTATION_SCHEMA_VERSION,
        "artifact_type": "candidate_annotation_resolution",
        "source_contract": {
            "path": str(source_contract_path),
            "sha256": contract_sha256,
        },
        "source_vote_ledger": {
            "path": str(ledger_path),
            "sha256": ledger_snapshot.sha256,
            "schema_version": LEDGER_SCHEMA_VERSION,
            "contract_sha256": ledger_header["contract_sha256"],
            "dataset_version": ledger_header.get("dataset_version"),
            "evidence_manifest_sha256": ledger_header.get("evidence_manifest_sha256"),
        },
        "source_dataset_manifest": dataset_binding["source"] if dataset_binding is not None else None,
        "evidence_hash_policy": EVIDENCE_HASH_POLICY,
        "linked_artifacts": {
            "adjudication_queue": ADJUDICATION_QUEUE_NAME,
            "derived_tracking_contract": TRACKING_CONTRACT_REPORT_NAME,
        },
        "derived_tracking_contract": {
            "path": TRACKING_CONTRACT_REPORT_NAME,
            "sha256": derived_contract_sha256,
        },
        "min_confidence": threshold,
        "summary": {
            "status": "complete",
            "candidate_count": len(candidate_ids),
            "vote_count": len(raw_votes),
            "confirmed_count": sum(item["status"] == "confirmed" for item in resolutions),
            "training_eligible_count": sum(bool(item["training_eligible"]) for item in resolutions),
            "adjudication_count": len(adjudication_queue),
        },
        "ledger_header": ledger_header,
        "vote_history": raw_votes,
        "resolutions": resolutions,
        "adjudication_queue": adjudication_queue,
    }
    queue_report = {
        "schema_version": ANNOTATION_SCHEMA_VERSION,
        "artifact_type": "candidate_annotation_adjudication_queue",
        "source_resolution": ANNOTATION_RESOLUTION_NAME,
        "candidate_count": len(adjudication_queue),
        "candidates": adjudication_queue,
    }
    _validate_finite_json(report, name="annotation resolution")
    _validate_finite_json(queue_report, name="adjudication queue")
    _validate_finite_json(derived_contract, name="derived tracking contract")
    input_snapshots = [contract_snapshot, ledger_snapshot]
    if dataset_binding is not None:
        input_snapshots.extend(dataset_binding["snapshots"])
    _verify_unchanged_snapshots(input_snapshots)
    _publish_reports(
        output_dir,
        (
            (ANNOTATION_RESOLUTION_NAME, report),
            (ADJUDICATION_QUEUE_NAME, queue_report),
            (TRACKING_CONTRACT_REPORT_NAME, derived_contract),
        ),
        preencoded={TRACKING_CONTRACT_REPORT_NAME: derived_contract_bytes},
    )
    return report


def _load_tracking_contract_snapshot(path: Path) -> tuple[dict[str, Any], _FileSnapshot]:
    raw_bytes, snapshot = _capture_file_snapshot(path, "source tracking contract")
    raw = _parse_json_object_bytes(raw_bytes, "source tracking contract")
    errors: list[str] = []
    if raw.get("schema_version") != TRACKING_CONTRACT_SCHEMA_VERSION:
        errors.append(f"schema_version: expected {TRACKING_CONTRACT_SCHEMA_VERSION}")
    if "generated_at" not in raw or (
        raw.get("generated_at") is not None and not isinstance(raw.get("generated_at"), str)
    ):
        errors.append("generated_at: required string or null")
    if not isinstance(raw.get("summary"), dict):
        errors.append("summary: required object")
    source_errors = raw.get("validation_errors")
    if not isinstance(source_errors, list) or not all(isinstance(item, str) for item in source_errors):
        errors.append("validation_errors: required string list")
        source_errors = []
    collections: dict[str, list[dict[str, Any]]] = {}
    for name in ("frames", "candidates", "classifications", "decisions"):
        value = raw.get(name)
        if not isinstance(value, list):
            errors.append(f"{name}: required list")
            value = []
        collections[name] = value
    contract = build_tracking_contract(source=raw.get("source"), **collections)
    errors.extend(str(error) for error in source_errors if error)
    errors.extend(str(error) for error in contract["validation_errors"])
    if errors:
        raise ValueError(f"invalid tracking contract: {'; '.join(_dedupe(errors))}")
    return contract, snapshot


def load_vote_ledger(
    path: Path,
    *,
    candidate_ids: set[str],
    contract_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], _FileSnapshot]:
    path = Path(path)
    raw_bytes, snapshot = _capture_file_snapshot(path, "source vote ledger")
    try:
        lines = raw_bytes.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError(f"could not decode vote ledger as UTF-8: {exc}") from exc

    records: list[tuple[int, dict[str, Any]]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"vote ledger line {line_number}: invalid finite JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"vote ledger line {line_number}: record must be an object")
        records.append((line_number, raw))
    if not records:
        raise ValueError("vote ledger must start with a ledger_header record")

    header_line, raw_header = records[0]
    ledger_header = _normalize_ledger_header(
        raw_header,
        line_number=header_line,
        contract_sha256=contract_sha256,
    )
    raw_votes: list[dict[str, Any]] = []
    normalized_votes: list[dict[str, Any]] = []
    vote_ids: set[str] = set()
    for line_number, raw in records[1:]:
        normalized = _normalize_vote(
            raw,
            line_number=line_number,
            candidate_ids=candidate_ids,
            ledger_header=ledger_header,
        )
        vote_id = normalized["vote_id"]
        if vote_id in vote_ids:
            raise ValueError(f"vote ledger line {line_number}: duplicate vote_id {vote_id!r}")
        vote_ids.add(vote_id)
        raw_votes.append(raw)
        normalized_votes.append(normalized)
    return ledger_header, raw_votes, normalized_votes, snapshot


def _normalize_ledger_header(
    raw: dict[str, Any],
    *,
    line_number: int,
    contract_sha256: str,
) -> dict[str, Any]:
    if raw.get("schema_version") != LEDGER_SCHEMA_VERSION:
        raise ValueError(f"vote ledger line {line_number}: schema_version must be {LEDGER_SCHEMA_VERSION!r}")
    if raw.get("record_type") != "ledger_header":
        raise ValueError(f"vote ledger line {line_number}: first record_type must be 'ledger_header'")
    bound_contract_sha256 = _sha256_text(raw.get("contract_sha256"), "contract_sha256", line_number)
    if bound_contract_sha256 != contract_sha256:
        raise ValueError("vote ledger contract_sha256 does not match the source tracking contract")
    result: dict[str, Any] = dict(raw)
    result.update(
        {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "record_type": "ledger_header",
            "contract_sha256": bound_contract_sha256,
        }
    )
    if "dataset_version" in raw:
        result["dataset_version"] = _required_text(raw.get("dataset_version"), "dataset_version", line_number)
    if "evidence_manifest_sha256" in raw:
        result["evidence_manifest_sha256"] = _sha256_text(
            raw.get("evidence_manifest_sha256"),
            "evidence_manifest_sha256",
            line_number,
        )
    if ("dataset_version" in result) != ("evidence_manifest_sha256" in result):
        raise ValueError(
            f"vote ledger line {line_number}: dataset_version and evidence_manifest_sha256 must appear together"
        )
    return result


def _normalize_vote(
    raw: dict[str, Any],
    *,
    line_number: int,
    candidate_ids: set[str],
    ledger_header: dict[str, Any],
) -> dict[str, Any]:
    if raw.get("schema_version") != LEDGER_SCHEMA_VERSION:
        raise ValueError(f"vote ledger line {line_number}: schema_version must be {LEDGER_SCHEMA_VERSION!r}")
    if raw.get("record_type") != "vote":
        raise ValueError(f"vote ledger line {line_number}: record_type must be 'vote'")
    vote_id = _required_text(raw.get("vote_id"), "vote_id", line_number)
    candidate_id = _required_text(raw.get("candidate_id"), "candidate_id", line_number)
    if candidate_id not in candidate_ids:
        raise ValueError(f"vote ledger line {line_number}: candidate_id references absent candidate {candidate_id!r}")
    stage = raw.get("stage")
    if stage not in _VOTE_STAGES:
        raise ValueError(f"vote ledger line {line_number}: stage must be one of {sorted(_VOTE_STAGES)}")
    reviewer_type = raw.get("reviewer_type")
    if reviewer_type not in _REVIEWER_TYPES:
        raise ValueError(f"vote ledger line {line_number}: reviewer_type must be one of {sorted(_REVIEWER_TYPES)}")
    annotator_id = _required_text(raw.get("annotator_id"), "annotator_id", line_number)
    fingerprint = _required_text(raw.get("fingerprint"), "fingerprint", line_number)
    label = raw.get("label")
    if label not in CLASSIFICATION_LABELS:
        raise ValueError(f"vote ledger line {line_number}: label must be one of {list(CLASSIFICATION_LABELS)}")
    confidence = _finite_probability(raw.get("confidence"), "confidence", line_number)
    blind = raw.get("blind")
    if not isinstance(blind, bool):
        raise ValueError(f"vote ledger line {line_number}: blind must be a boolean")
    created_at = _required_text(raw.get("created_at"), "created_at", line_number)
    _validate_timestamp(created_at, line_number=line_number)
    result = {
        "vote_id": vote_id,
        "candidate_id": candidate_id,
        "stage": stage,
        "reviewer_type": reviewer_type,
        "annotator_id": annotator_id,
        "fingerprint": fingerprint,
        "label": label,
        "confidence": confidence,
        "blind": blind,
        "created_at": created_at,
    }
    dataset_bound = "dataset_version" in ledger_header
    sample_bound = "sample_id" in raw or "evidence_sha256" in raw or "dataset_version" in raw
    if dataset_bound and not all(name in raw for name in ("dataset_version", "sample_id", "evidence_sha256")):
        raise ValueError(
            f"vote ledger line {line_number}: dataset-bound votes require dataset_version, sample_id, and evidence_sha256"
        )
    if sample_bound and not dataset_bound:
        raise ValueError(f"vote ledger line {line_number}: sample evidence requires a dataset-bound ledger_header")
    if "dataset_version" in raw:
        dataset_version = _required_text(raw.get("dataset_version"), "dataset_version", line_number)
        if dataset_version != ledger_header.get("dataset_version"):
            raise ValueError(f"vote ledger line {line_number}: dataset_version does not match ledger_header")
        result["dataset_version"] = dataset_version
    for name in ("sample_id", "evidence_sha256"):
        if name not in raw:
            continue
        if name == "sample_id":
            result[name] = _required_text(raw.get(name), name, line_number)
        else:
            result[name] = _sha256_text(raw.get(name), name, line_number)
    return result


def _resolve_existing_confirmed(
    candidate_id: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    labels = {str(row["label"]) for row in rows}
    origins = sorted({str(row["label_origin"]) for row in rows})
    if len(labels) != 1:
        return _resolution(
            candidate_id,
            status="existing_confirmed_conflict",
            label="unknown",
            label_origin=None,
            confidence=None,
            resolution_source="existing_contract_conflict",
            training_eligible=False,
            reasons=["existing_confirmed_label_conflict"],
            primary_vote_ids=[],
            adjudication_vote_ids=[],
            existing_confirmed_origins=origins,
        )
    label = next(iter(labels))
    strongest_origin = "human_confirmed" if "human_confirmed" in origins else "ai_confirmed"
    strongest_rows = [row for row in rows if row["label_origin"] == strongest_origin]
    confidences = [float(row["confidence"]) for row in strongest_rows if "confidence" in row]
    return _resolution(
        candidate_id,
        status="confirmed",
        label=label,
        label_origin=strongest_origin,
        confidence=min(confidences) if confidences else None,
        resolution_source="existing_contract",
        training_eligible=True,
        reasons=[],
        primary_vote_ids=[],
        adjudication_vote_ids=[],
        existing_confirmed_origins=origins,
    )


def _resolve_votes(
    candidate_id: str,
    votes: list[dict[str, Any]],
    *,
    min_confidence: float,
) -> dict[str, Any]:
    primary = [vote for vote in votes if vote["stage"] == "primary"]
    adjudication = [vote for vote in votes if vote["stage"] == "adjudication"]
    primary_ids = [str(vote["vote_id"]) for vote in primary]
    adjudication_ids = [str(vote["vote_id"]) for vote in adjudication]
    duplicate_reasons = _duplicate_identity_reasons(votes)

    if adjudication:
        reasons: list[str] = []
        if len(adjudication) != 1:
            reasons.append("adjudication_vote_count")
        judge = adjudication[0] if len(adjudication) == 1 else None
        if judge is not None:
            if judge["reviewer_type"] != "human":
                reasons.append("adjudicator_must_be_human")
            if judge["confidence"] < min_confidence:
                reasons.append("below_confidence_threshold")
            if judge["annotator_id"] in {vote["annotator_id"] for vote in primary}:
                reasons.append("duplicate_annotator_id")
            if judge["fingerprint"] in {vote["fingerprint"] for vote in primary}:
                reasons.append("duplicate_fingerprint")
        if not reasons and judge is not None:
            return _resolution(
                candidate_id,
                status="confirmed",
                label=str(judge["label"]),
                label_origin="human_confirmed",
                confidence=float(judge["confidence"]),
                resolution_source="human_adjudication",
                training_eligible=True,
                reasons=[],
                primary_vote_ids=primary_ids,
                adjudication_vote_ids=adjudication_ids,
            )
        return _unresolved_resolution(
            candidate_id,
            reasons,
            primary_vote_ids=primary_ids,
            adjudication_vote_ids=adjudication_ids,
        )

    reasons = list(duplicate_reasons)
    if len(primary) != 2:
        reasons.append("primary_vote_count")
    if any(not vote["blind"] for vote in primary):
        reasons.append("primary_vote_not_blind")
    if any(vote["confidence"] < min_confidence for vote in primary):
        reasons.append("below_confidence_threshold")
    if any(vote["label"] == "unknown" for vote in primary):
        reasons.append("unknown_primary_label")
    labels = {str(vote["label"]) for vote in primary}
    if len(primary) == 2 and len(labels) != 1:
        reasons.append("primary_label_disagreement")
    reviewer_types = {str(vote["reviewer_type"]) for vote in primary}
    if len(primary) == 2 and len(reviewer_types) != 1:
        reasons.append("mixed_primary_reviewer_types")
    if reasons:
        return _unresolved_resolution(
            candidate_id,
            reasons,
            primary_vote_ids=primary_ids,
            adjudication_vote_ids=[],
        )

    reviewer_type = next(iter(reviewer_types))
    label_origin = "ai_confirmed" if reviewer_type == "ai" else "human_confirmed"
    return _resolution(
        candidate_id,
        status="confirmed",
        label=next(iter(labels)),
        label_origin=label_origin,
        confidence=min(float(vote["confidence"]) for vote in primary),
        resolution_source="blind_primary_consensus",
        training_eligible=True,
        reasons=[],
        primary_vote_ids=primary_ids,
        adjudication_vote_ids=[],
    )


def _duplicate_identity_reasons(votes: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    if any(count > 1 for count in Counter(vote["annotator_id"] for vote in votes).values()):
        reasons.append("duplicate_annotator_id")
    if any(count > 1 for count in Counter(vote["fingerprint"] for vote in votes).values()):
        reasons.append("duplicate_fingerprint")
    return reasons


def _unresolved_resolution(
    candidate_id: str,
    reasons: list[str],
    *,
    primary_vote_ids: list[str],
    adjudication_vote_ids: list[str],
) -> dict[str, Any]:
    return _resolution(
        candidate_id,
        status="pending_adjudication",
        label="unknown",
        label_origin="prelabel",
        confidence=0.0,
        resolution_source="unresolved_votes",
        training_eligible=False,
        reasons=_dedupe(reasons or ["primary_vote_count"]),
        primary_vote_ids=primary_vote_ids,
        adjudication_vote_ids=adjudication_vote_ids,
    )


def _resolution(
    candidate_id: str,
    *,
    status: str,
    label: str,
    label_origin: str | None,
    confidence: float | None,
    resolution_source: str,
    training_eligible: bool,
    reasons: list[str],
    primary_vote_ids: list[str],
    adjudication_vote_ids: list[str],
    existing_confirmed_origins: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "candidate_id": candidate_id,
        "status": status,
        "label": label,
        "label_origin": label_origin,
        "confidence": confidence,
        "resolution_source": resolution_source,
        "training_eligible": training_eligible,
        "reasons": reasons,
        "primary_vote_ids": primary_vote_ids,
        "adjudication_vote_ids": adjudication_vote_ids,
    }
    if existing_confirmed_origins is not None:
        result["existing_confirmed_origins"] = existing_confirmed_origins
    return result


def _has_unknown_classification(classifications: list[dict[str, Any]], candidate_id: str) -> bool:
    return any(row.get("candidate_id") == candidate_id and row.get("label") == "unknown" for row in classifications)


def _validate_dataset_evidence(
    manifest_path: Path | None,
    *,
    ledger_header: dict[str, Any],
    votes: list[dict[str, Any]],
    candidate_ids: set[str],
    contract_sha256: str,
) -> dict[str, Any] | None:
    has_dataset_binding = "dataset_version" in ledger_header or "evidence_manifest_sha256" in ledger_header
    if not votes and not has_dataset_binding:
        return None
    if manifest_path is None:
        raise ValueError("visual votes require a real candidate dataset manifest")
    if not has_dataset_binding:
        raise ValueError("dataset manifest requires dataset_version and evidence_manifest_sha256 in ledger_header")

    manifest_path = Path(manifest_path)
    manifest_bytes, manifest_snapshot = _capture_file_snapshot(manifest_path, "candidate dataset manifest")
    manifest_sha256 = manifest_snapshot.sha256
    if manifest_sha256 != ledger_header.get("evidence_manifest_sha256"):
        raise ValueError("ledger_header evidence_manifest_sha256 does not match the candidate dataset manifest")
    manifest = _parse_json_object_bytes(manifest_bytes, "candidate dataset manifest")
    if manifest.get("schema_version") != "1.0" or manifest.get("artifact_type") != "candidate_dataset":
        raise ValueError("candidate dataset manifest must use candidate_dataset schema 1.0")
    dataset_version = _plain_required_text(manifest.get("dataset_version"), "dataset manifest dataset_version")
    if dataset_version != ledger_header.get("dataset_version"):
        raise ValueError("ledger_header dataset_version does not match the candidate dataset manifest")
    contract_descriptor = manifest.get("contract")
    if not isinstance(contract_descriptor, dict) or contract_descriptor.get("sha256") != contract_sha256:
        raise ValueError("candidate dataset manifest contract sha256 does not match the source tracking contract")
    if manifest.get("frame_offsets") != [-2, -1, 0, 1, 2]:
        raise ValueError("candidate dataset manifest must bind the five frame offsets [-2,-1,0,1,2]")
    expected_tensor_contract = {
        "color_space": "RGB",
        "dtype": "uint8",
        "tight_shape": [5, 3, 64, 64],
        "context_shape": [5, 3, 128, 128],
        "markup": False,
    }
    if manifest.get("tensor_contract") != expected_tensor_contract:
        raise ValueError("candidate dataset manifest tensor contract is incompatible with annotation evidence")

    raw_samples = manifest.get("samples")
    if not isinstance(raw_samples, list) or not raw_samples:
        raise ValueError("candidate dataset manifest must contain samples")
    samples_by_candidate: dict[str, dict[str, Any]] = {}
    sample_ids: set[str] = set()
    evidence_snapshots = [manifest_snapshot]
    manifest_root = manifest_path.parent.resolve()
    for index, sample in enumerate(raw_samples):
        if not isinstance(sample, dict):
            raise ValueError(f"candidate dataset sample {index} must be an object")
        candidate_id = _plain_required_text(sample.get("candidate_id"), f"samples[{index}].candidate_id")
        sample_id = _plain_required_text(sample.get("sample_id"), f"samples[{index}].sample_id")
        if candidate_id in samples_by_candidate:
            raise ValueError(f"candidate dataset manifest has duplicate candidate_id {candidate_id!r}")
        if sample_id in sample_ids:
            raise ValueError(f"candidate dataset manifest has duplicate sample_id {sample_id!r}")
        sample_ids.add(sample_id)
        evidence_snapshots.extend(_verify_sample_artifacts(sample, sample_id=sample_id, manifest_root=manifest_root))
        samples_by_candidate[candidate_id] = sample
    if set(samples_by_candidate) != candidate_ids:
        raise ValueError("candidate dataset manifest candidate IDs do not match the source tracking contract")
    summary = manifest.get("summary")
    if not isinstance(summary, dict) or summary.get("sample_count") != len(raw_samples):
        raise ValueError("candidate dataset manifest sample_count does not match samples")

    for vote in votes:
        sample = samples_by_candidate[vote["candidate_id"]]
        expected_sample_id = sample["sample_id"]
        if vote.get("sample_id") != expected_sample_id:
            raise ValueError(
                f"vote {vote['vote_id']!r} sample_id does not match candidate {vote['candidate_id']!r} evidence"
            )
        expected_evidence_sha256 = sample_evidence_sha256(sample)
        if vote.get("evidence_sha256") != expected_evidence_sha256:
            raise ValueError(f"vote {vote['vote_id']!r} evidence_sha256 does not match sample artifacts")

    return {
        "source": {
            "path": str(manifest_path),
            "sha256": manifest_sha256,
            "dataset_version": dataset_version,
            "sample_count": len(raw_samples),
        },
        "snapshots": evidence_snapshots,
    }


def _verify_sample_artifacts(
    sample: dict[str, Any],
    *,
    sample_id: str,
    manifest_root: Path,
) -> list[_FileSnapshot]:
    artifacts = sample.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError(f"candidate dataset sample {sample_id!r} artifacts must be an object")
    snapshots: list[_FileSnapshot] = []
    for name in _EVIDENCE_ARTIFACTS:
        descriptor = artifacts.get(name)
        if not isinstance(descriptor, dict):
            raise ValueError(f"candidate dataset sample {sample_id!r} missing {name!r} artifact")
        relative_text = _plain_required_text(descriptor.get("path"), f"sample {sample_id} {name} path")
        relative_path = Path(relative_text)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"candidate dataset sample {sample_id!r} artifact path must be contained and relative")
        artifact_path = (manifest_root / relative_path).resolve()
        if not artifact_path.is_relative_to(manifest_root):
            raise ValueError(f"candidate dataset sample {sample_id!r} artifact is missing or escapes the dataset")
        expected_sha256 = _plain_sha256_text(descriptor.get("sha256"), f"sample {sample_id} {name} sha256")
        _, snapshot = _capture_file_snapshot(artifact_path, f"candidate evidence {sample_id} {name}")
        if snapshot.sha256 != expected_sha256:
            raise ValueError(f"candidate dataset sample {sample_id!r} {name} artifact sha256 mismatch")
        snapshots.append(snapshot)
    return snapshots


def _publish_reports(
    output_dir: Path,
    reports: tuple[tuple[str, dict[str, Any]], ...],
    *,
    preencoded: dict[str, bytes] | None = None,
) -> None:
    names = [name for name, _ in reports]
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    if duplicates:
        raise ValueError(f"annotation artifact set contains duplicate names: {duplicates}")
    expected_names = set(_ANNOTATION_ARTIFACT_ORDER)
    actual_names = set(names)
    missing = sorted(expected_names - actual_names)
    unexpected = sorted(actual_names - expected_names)
    if missing or unexpected:
        raise ValueError(f"annotation artifact set has missing={missing}, unexpected={unexpected}")
    if preencoded is not None and not set(preencoded).issubset(expected_names):
        raise ValueError("preencoded annotation artifacts contain an unexpected name")

    output_dir.mkdir(parents=True, exist_ok=True)
    payloads = dict(reports)
    staged_by_name: dict[str, tuple[Path, Path]] = {}
    try:
        for name in _ANNOTATION_ARTIFACT_ORDER:
            payload = payloads[name]
            final_path = output_dir / name
            descriptor, raw_path = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=output_dir)
            temporary_path = Path(raw_path)
            staged_by_name[name] = (temporary_path, final_path)
            os.close(descriptor)
            encoded = preencoded[name] if preencoded is not None and name in preencoded else _json_bytes(payload)
            temporary_path.write_bytes(encoded)
        staged = [staged_by_name[name] for name in _ANNOTATION_ARTIFACT_ORDER]
        _publish_artifact_set(staged)
    finally:
        for temporary_path, _ in staged_by_name.values():
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _publish_artifact_set(staged: list[tuple[Path, Path]]) -> None:
    backups: dict[Path, Path] = {}
    published: list[Path] = []
    try:
        for _, final_path in staged:
            if final_path.exists():
                backup = final_path.with_name(f".{final_path.name}.{uuid.uuid4().hex}.bak")
                backups[final_path] = backup
                os.replace(final_path, backup)
        for temporary_path, final_path in staged:
            published.append(final_path)
            os.replace(temporary_path, final_path)
    except BaseException:
        for final_path in reversed(published):
            final_path.unlink(missing_ok=True)
        for final_path, backup in reversed(tuple(backups.items())):
            if backup.exists():
                os.replace(backup, final_path)
        raise
    else:
        for backup in backups.values():
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass


def _confidence_threshold(value: Any) -> float:
    parsed = _finite_probability(value, "min_confidence", 0)
    return parsed


def _finite_probability(value: Any, name: str, line_number: int) -> float:
    prefix = f"vote ledger line {line_number}: " if line_number else ""
    if isinstance(value, bool):
        raise ValueError(f"{prefix}{name} must be a finite number between 0 and 1")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{prefix}{name} must be a finite number between 0 and 1") from exc
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise ValueError(f"{prefix}{name} must be a finite number between 0 and 1")
    return parsed


def _required_text(value: Any, name: str, line_number: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"vote ledger line {line_number}: {name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"vote ledger line {line_number}: {name} must not have surrounding whitespace")
    return value


def _plain_required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a non-empty string without surrounding whitespace")
    return value


def _sha256_text(value: Any, name: str, line_number: int) -> str:
    text = _required_text(value, name, line_number)
    if len(text) != 64 or any(character not in hexdigits for character in text):
        raise ValueError(f"vote ledger line {line_number}: {name} must be a 64-character SHA-256 hex digest")
    return text.lower()


def _plain_sha256_text(value: Any, name: str) -> str:
    text = _plain_required_text(value, name)
    if len(text) != 64 or any(character not in hexdigits for character in text):
        raise ValueError(f"{name} must be a 64-character SHA-256 hex digest")
    return text.lower()


def _validate_timestamp(value: str, *, line_number: int) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"vote ledger line {line_number}: created_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"vote ledger line {line_number}: created_at must include a timezone")


def _validate_finite_json(payload: Any, *, name: str) -> None:
    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not finite JSON: {exc}") from exc


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite constant {value}")


def _capture_file_snapshot(path: Path, label: str) -> tuple[bytes, _FileSnapshot]:
    path = Path(path)
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"could not read {label}: {path}: {exc}") from exc
    return raw_bytes, _FileSnapshot(path=path, sha256=hashlib.sha256(raw_bytes).hexdigest(), label=label)


def _parse_json_object_bytes(raw_bytes: bytes, label: str) -> dict[str, Any]:
    try:
        text = raw_bytes.decode("utf-8")
        payload = json.loads(text, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid finite UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _verify_unchanged_snapshots(snapshots: list[_FileSnapshot]) -> None:
    for snapshot in snapshots:
        try:
            current_sha256 = _sha256_file(snapshot.path)
        except OSError as exc:
            raise ValueError(f"{snapshot.label} changed during annotation resolution: {exc}") from exc
        if current_sha256 != snapshot.sha256:
            raise ValueError(f"{snapshot.label} changed during annotation resolution")


def _contract_file_path(path: Path) -> Path:
    return path / TRACKING_CONTRACT_REPORT_NAME if path.is_dir() else path


def _same_path(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve()


def _reject_input_output_aliases(
    *,
    source_contract_path: Path,
    ledger_path: Path,
    dataset_manifest_path: Path | None,
    final_paths: tuple[Path, ...],
) -> None:
    input_paths: list[tuple[str, Path]] = [
        ("source tracking contract", source_contract_path),
        ("source vote ledger", ledger_path),
    ]
    if dataset_manifest_path is not None:
        input_paths.append(("source candidate dataset manifest", dataset_manifest_path))
    for input_name, input_path in input_paths:
        if any(_same_path(input_path, final_path) for final_path in final_paths):
            raise ValueError(f"output artifacts must not overwrite the {input_name}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
