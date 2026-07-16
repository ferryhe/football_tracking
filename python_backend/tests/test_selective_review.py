from __future__ import annotations

import hashlib
import io
import json
import math
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import patch

from test_selective_policy import _write_inputs

from football_tracking.candidate_annotations import resolve_candidate_annotations
from football_tracking.selective_policy import (
    AUDIT_ALGORITHM,
    DECISION_ALGORITHM,
    INFERENTIAL_UNIT,
    INFERENTIAL_UNIT_ALGORITHM,
    POLICY_VERSION_ALGORITHM,
    ROLE_ASSIGNMENT_ALGORITHM,
    ROLE_COMPONENT_ID_ALGORITHM,
    THRESHOLD_ALGORITHM,
    SelectivePolicyConfig,
    SelectivePolicyError,
    _binomial_lower_tail,
    _exact_binomial_upper_bound,
    _holm_rejections,
    _minimum_zero_error_sample,
    _qualification_evidence_summary,
    _wilson_upper_bound,
    fit_selective_policy,
    validate_selective_decisions_binding,
)
from football_tracking.selective_review import (
    SelectiveReviewError,
    _select_review_candidates,
    _selection_report,
    build_cli_main,
    build_review_windows,
    build_selective_review_queue,
    materialize_cli_main,
    materialize_selective_review_actions,
)
from football_tracking.tracking_benchmark import build_benchmark_report
from football_tracking.tracking_contracts import CLASSIFICATION_LABELS, build_tracking_contract


class _PrematureEofReader:
    def __init__(self, handle: object, byte_limit: int) -> None:
        self._handle = handle
        self._remaining = byte_limit

    def __enter__(self) -> _PrematureEofReader:
        return self

    def __exit__(self, *args: object) -> None:
        self._handle.close()

    def fileno(self) -> int:
        return self._handle.fileno()

    def read(self, size: int = -1) -> bytes:
        if self._remaining == 0:
            return b""
        requested = self._remaining if size < 0 else min(size, self._remaining)
        chunk = self._handle.read(requested)
        self._remaining -= len(chunk)
        return chunk


class SelectiveReviewCaptureTests(unittest.TestCase):
    def test_queue_and_actions_loader_hashes_the_exact_captured_bytes(self) -> None:
        from football_tracking import selective_review

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            target = root / "actions.json"
            alternate = root / "alternate-actions.json"
            target_bytes = b'{"marker":"A"}\n'
            alternate_bytes = b'{"marker":"B"}\n'
            target.write_bytes(target_bytes)
            alternate.write_bytes(alternate_bytes)
            original_open = Path.open

            def open_captured_version(path: Path, *args: object, **kwargs: object) -> object:
                captured_path = alternate if path.resolve() == target.resolve() else path
                return original_open(captured_path, *args, **kwargs)

            with patch.object(Path, "open", new=open_captured_version):
                value, snapshot = selective_review._load_json_snapshot(target, "selective review actions")

            self.assertEqual({"marker": "B"}, value)
            self.assertEqual(hashlib.sha256(alternate_bytes).hexdigest(), snapshot.sha256)
            self.assertEqual(len(alternate_bytes), snapshot.size)
            self.assertEqual(target_bytes, target.read_bytes())

    def test_queue_and_actions_loader_rejects_premature_eof_and_invalid_utf8(self) -> None:
        from football_tracking import selective_review

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            path = root / "queue.json"
            payload = b'{"marker":"complete"}\n'
            path.write_bytes(payload)
            original_open = Path.open

            def open_truncated(path_value: Path, *args: object, **kwargs: object) -> object:
                handle = original_open(path_value, *args, **kwargs)
                if path_value.resolve() == path.resolve():
                    return _PrematureEofReader(handle, len(payload) - 2)
                return handle

            with patch.object(Path, "open", new=open_truncated):
                with self.assertRaisesRegex(SelectiveReviewError, "ended early"):
                    selective_review._load_json_snapshot(path, "selective review queue")

            path.write_bytes(b'{"marker":"\xff"}\n')
            with self.assertRaisesRegex(SelectiveReviewError, "invalid selective review queue.*utf-8"):
                selective_review._load_json_snapshot(path, "selective review queue")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _decisions_content_sha256(report: dict[str, object]) -> str:
    content = {key: value for key, value in report.items() if key not in {"generated_at", "policy_version"}}
    return _canonical_sha256(content)


def _decisions_summary(rows: list[dict[str, object]]) -> dict[str, int]:
    return {
        "candidate_count": len(rows),
        "accept_count": sum(row["decision"] == "accept" for row in rows),
        "reject_count": sum(row["decision"] == "reject" for row in rows),
        "abstain_count": sum(row["decision"] == "abstain" for row in rows),
        "forced_abstain_count": sum(bool(row["forced_abstain_reasons"]) for row in rows),
        "evaluation_holdout_count": sum(row["decision_scope"] == "evaluation_only" for row in rows),
        "application_count": sum(row["decision_scope"] == "application" for row in rows),
        "preserved_existing_decision_count": sum(bool(row["existing_decision_preserved"]) for row in rows),
        "pending_application_count": sum(
            row["decision_scope"] == "application"
            and not row["applied_to_contract"]
            and not row["existing_decision_preserved"]
            for row in rows
        ),
    }


def _synthetic_qualified_evidence(
    config: dict[str, object],
    thresholds: dict[str, float],
    calibration_candidate_ids: list[str],
    audit_candidate_ids: list[str],
) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    calibration_component_count = len(calibration_candidate_ids)
    audit_component_count = len(audit_candidate_ids)
    accepted_count = 183
    accept_p = _binomial_lower_tail(0, accepted_count, 1.0 - config["accept_precision_target"])
    reject_p = _binomial_lower_tail(0, calibration_component_count, config["false_reject_target"])
    hypotheses = [("accept-00", accept_p), ("reject-00", reject_p)]
    rejected = _holm_rejections(hypotheses, alpha=config["fwer_alpha"])
    cluster_gate = {
        "method": "heterogeneity_descriptive_diagnostic_v2",
        "purpose": "diagnostic_only",
        "affects_qualification": False,
        "qualification_scope": "fixed_aggregate_audit_cohort",
        "per_cluster_statistical_guarantee": "none",
        "minimum_accepted_per_cluster": config["min_cluster_accepted"],
        "minimum_true_balls_per_cluster": config["min_cluster_true_balls"],
        "failed_clusters": [],
        "passed": True,
    }
    pair = {
        "accept_hypothesis_id": "accept-00",
        "reject_hypothesis_id": "reject-00",
        "accept_threshold": thresholds["accept"],
        "reject_threshold": thresholds["reject"],
        "accepted_count": accepted_count,
        "automated_count": accepted_count,
        "accepted_component_count": accepted_count,
        "automated_component_count": accepted_count,
        "cluster_gate": cluster_gate,
    }
    calibration_dimension_counts = {
        name: 2 for name in ("variant_id", "video_sha256", "group_id", "split_group", "temporal_group")
    }
    audit_dimension_counts = {
        name: 2 for name in ("variant_id", "video_sha256", "group_id", "split_group", "temporal_group")
    }
    cluster_diagnostics = {
        "video_sha256": [],
        "group_id": [],
        "split_group": [],
        "temporal_group": [],
    }
    calibration = {
        "status": "certified",
        "certified": True,
        "method": THRESHOLD_ALGORITHM,
        "inferential_unit": INFERENTIAL_UNIT,
        "inferential_unit_algorithm": INFERENTIAL_UNIT_ALGORITHM,
        "calibration_count": calibration_component_count,
        "calibration_component_count": calibration_component_count,
        "calibration_candidate_ids": calibration_candidate_ids,
        "evidence_dimension_counts": calibration_dimension_counts,
        "independent_component_gate": {
            "observed": calibration_component_count,
            "minimum": config["min_independent_components"],
            "passed": True,
        },
        "accept_threshold_grid": [thresholds["accept"]],
        "reject_threshold_grid": [thresholds["reject"]],
        "predeclared_pair_count": 1,
        "component_hypothesis_count": 2,
        "holm_rejected_hypotheses": sorted(rejected),
        "minimum_zero_error_samples": {
            "accept": _minimum_zero_error_sample(1.0 - config["accept_precision_target"], config["fwer_alpha"], 2),
            "reject": _minimum_zero_error_sample(config["false_reject_target"], config["fwer_alpha"], 2),
        },
        "selected_hypothesis": pair,
        "accept_hypotheses": [
            {
                "hypothesis_id": "accept-00",
                "threshold": thresholds["accept"],
                "inferential_unit": INFERENTIAL_UNIT,
                "n": accepted_count,
                "selected_component_count": accepted_count,
                "selected_count": accepted_count,
                "error_count": 0,
                "p_value": accept_p,
            }
        ],
        "reject_hypotheses": [
            {
                "hypothesis_id": "reject-00",
                "threshold": thresholds["reject"],
                "inferential_unit": INFERENTIAL_UNIT,
                "n": calibration_component_count,
                "true_ball_component_count": calibration_component_count,
                "selected_count": 0,
                "true_ball_count": calibration_component_count,
                "error_count": 0,
                "p_value": reject_p,
            }
        ],
        "certified_pairs": [pair],
        "cluster_diagnostics": cluster_diagnostics,
        "hypothesis_family_sha256": _canonical_sha256(
            [{"id": identifier, "p_value": p_value} for identifier, p_value in sorted(hypotheses)]
        ),
    }
    evaluations = [
        {
            "candidate_id": candidate_id,
            "truth": "match_ball",
            "truth_origin": "human_confirmed",
            "decision": "accept" if index < accepted_count else "abstain",
            "confidence": 0.9 if index < accepted_count else 0.5,
        }
        for index, candidate_id in enumerate(audit_candidate_ids)
    ]
    benchmark = build_benchmark_report(candidate_evaluations=evaluations)
    benchmark.pop("generated_at")
    endpoint_alpha = config["fwer_alpha"] / 2.0
    accept_exact = _exact_binomial_upper_bound(0, accepted_count, alpha=endpoint_alpha)
    reject_exact = _exact_binomial_upper_bound(0, audit_component_count, alpha=endpoint_alpha)
    audit = {
        "status": "qualified",
        "qualified": True,
        "qualification_scope": "fixed_aggregate_audit_cohort",
        "inferential_unit": INFERENTIAL_UNIT,
        "inferential_unit_algorithm": INFERENTIAL_UNIT_ALGORITHM,
        "audit_component_count": audit_component_count,
        "evidence_dimension_counts": audit_dimension_counts,
        "fixed_thresholds": thresholds,
        "benchmark": benchmark,
        "reconciled": True,
        "point_targets_passed": True,
        "one_sided_confidence": {
            "qualification_method": AUDIT_ALGORITHM,
            "inferential_unit": INFERENTIAL_UNIT,
            "accepted_component_count": accepted_count,
            "true_ball_component_count": audit_component_count,
            "familywise_alpha": config["fwer_alpha"],
            "per_endpoint_alpha": endpoint_alpha,
            "accept_error_exact_upper": accept_exact,
            "false_reject_exact_upper": reject_exact,
            "accept_error_upper": _wilson_upper_bound(0, accepted_count, alpha=endpoint_alpha),
            "false_reject_upper": _wilson_upper_bound(0, audit_component_count, alpha=endpoint_alpha),
            "wilson_is_diagnostic_only": True,
            "scope": "fixed_aggregate_audit_cohort",
            "passed": True,
        },
        "sample_gates": {
            "accepted_components": {
                "observed": accepted_count,
                "minimum": config["min_audit_accepted"],
                "passed": True,
            },
            "true_ball_components": {
                "observed": audit_component_count,
                "minimum": config["min_audit_true_balls"],
                "passed": True,
            },
            "independent_components": {
                "observed": audit_component_count,
                "minimum": config["min_independent_components"],
                "passed": True,
            },
        },
        "cluster_gate": cluster_gate,
        "slices": [],
        "cluster_diagnostics": cluster_diagnostics,
    }
    inferential_unit = {
        "name": INFERENTIAL_UNIT,
        "algorithm": INFERENTIAL_UNIT_ALGORITHM,
        "calibration_component_count": calibration_component_count,
        "audit_component_count": audit_component_count,
    }
    qualification_evidence = _qualification_evidence_summary(calibration, audit)
    return calibration, audit, inferential_unit, qualification_evidence


def _seal_policy_decisions(
    policy_path: Path,
    decisions_path: Path,
    *,
    policy: dict[str, object] | None = None,
) -> None:
    if policy is None:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    decisions["decision_algorithm"] = DECISION_ALGORITHM
    decisions["summary"] = _decisions_summary(decisions["decisions"])
    content_sha256 = _decisions_content_sha256(decisions)
    config = asdict(SelectivePolicyConfig())
    thresholds = {"accept": 0.75, "reject": 0.75}
    rules = {
        "min_top_margin": config["min_top_margin"],
        "conflict_margin": config["conflict_margin"],
        "max_unknown_probability": config["max_unknown_probability"],
        "confirmed_unknown": "force_abstain",
        "confirmed_conflict": "force_abstain",
        "existing_decision": "preserve_and_abstain",
        "top_unknown": "force_abstain",
    }
    targets = {
        "auto_accept_precision_min": config["accept_precision_target"],
        "true_ball_false_reject_rate_max": config["false_reject_target"],
        "fwer_alpha": config["fwer_alpha"],
    }
    calibration_candidate_ids = sorted(
        row["candidate_id"] for row in decisions["decisions"] if row.get("policy_role") == "policy_calibration"
    )
    audit_candidate_ids = sorted(
        row["candidate_id"] for row in decisions["decisions"] if row.get("policy_role") == "policy_audit"
    )
    application_candidate_ids = sorted(
        row["candidate_id"] for row in decisions["decisions"] if row.get("policy_role") is None
    )
    calibration, audit, inferential_unit, qualification_evidence = _synthetic_qualified_evidence(
        config,
        thresholds,
        calibration_candidate_ids,
        audit_candidate_ids,
    )
    evaluation_cohorts = {
        "calibration_candidate_ids": calibration_candidate_ids,
        "audit_candidate_ids": audit_candidate_ids,
        "application_candidate_ids": application_candidate_ids,
    }
    version_inputs = {
        "schema_version": "1.0",
        "version_algorithm": POLICY_VERSION_ALGORITHM,
        "algorithm_versions": {
            "threshold_selection": THRESHOLD_ALGORITHM,
            "fixed_audit": AUDIT_ALGORITHM,
            "application_decisions": DECISION_ALGORITHM,
            "role_component_identity": ROLE_COMPONENT_ID_ALGORITHM,
            "role_assignment": ROLE_ASSIGNMENT_ALGORITHM,
            "inferential_unit": INFERENTIAL_UNIT_ALGORITHM,
        },
        "inferential_unit": inferential_unit,
        "evaluation_cohorts": evaluation_cohorts,
        "config": config,
        "qualification": {
            "qualified": True,
            "policy_status": "qualified",
            "acceptance_status": "qualified",
            "calibration_status": "certified",
            "calibration_certified": True,
            "audit_status": "qualified",
            "audit_qualified": True,
        },
        "qualification_evidence": qualification_evidence,
        "thresholds": thresholds,
        "rules": rules,
        "targets": targets,
        "lineage": policy.get("lineage"),
        "calibration_sha256": _canonical_sha256(calibration),
        "audit_sha256": _canonical_sha256(audit),
        "decisions_content_sha256": content_sha256,
    }
    policy_version = _canonical_sha256(version_inputs)
    decisions["policy_version"] = policy_version
    _write_json(decisions_path, decisions)
    policy["policy_version"] = policy_version
    policy["version_inputs"] = version_inputs
    policy["inferential_unit"] = inferential_unit
    policy["evaluation_cohorts"] = evaluation_cohorts
    policy["qualification_evidence"] = qualification_evidence
    policy["thresholds"] = thresholds
    policy["rules"] = rules
    policy["targets"] = targets
    policy["calibration"] = calibration
    policy["audit"] = audit
    policy["decisions_artifact"] = {
        "path": "selective_decisions.v1.json",
        "sha256": _sha256(decisions_path),
        "content_sha256": content_sha256,
    }
    _write_json(policy_path, policy)


def _reseal_policy_decisions(policy_path: Path, decisions_path: Path, policy: dict[str, object]) -> None:
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    policy["qualification_evidence"] = _qualification_evidence_summary(policy["calibration"], policy["audit"])
    policy["version_inputs"]["qualification_evidence"] = deepcopy(policy["qualification_evidence"])
    policy["version_inputs"]["inferential_unit"] = deepcopy(policy["inferential_unit"])
    policy["version_inputs"]["evaluation_cohorts"] = deepcopy(policy["evaluation_cohorts"])
    policy["version_inputs"]["calibration_sha256"] = _canonical_sha256(policy["calibration"])
    policy["version_inputs"]["audit_sha256"] = _canonical_sha256(policy["audit"])
    decisions["summary"] = _decisions_summary(decisions["decisions"])
    content_sha256 = _decisions_content_sha256(decisions)
    policy["version_inputs"]["decisions_content_sha256"] = content_sha256
    policy_version = _canonical_sha256(policy["version_inputs"])
    decisions["policy_version"] = policy_version
    _write_json(decisions_path, decisions)
    policy["policy_version"] = policy_version
    policy["decisions_artifact"]["sha256"] = _sha256(decisions_path)
    policy["decisions_artifact"]["content_sha256"] = content_sha256
    _write_json(policy_path, policy)


class _Fixture:
    """Production-shaped selective-policy fixture used at the review security boundary."""

    def __init__(
        self,
        root: Path,
        *,
        fps_by_variant: dict[str, float | None],
        extra_audits: int = 0,
        extra_uncertain: int = 0,
        extra_holdouts: int = 0,
        audit_frame_step: int = 300,
        include_application_candidates: bool = True,
    ) -> None:
        root.mkdir(parents=True, exist_ok=True)
        inputs = _write_inputs(
            root,
            calibration_per_class=368,
            audit_per_class=368,
            include_application_cases=include_application_candidates,
        )
        source_contract_path = root / "source-contract.json"
        source_contract = json.loads(source_contract_path.read_text(encoding="utf-8"))
        resolved_contract = json.loads(inputs["resolved_contract_path"].read_text(encoding="utf-8"))
        dataset = json.loads(inputs["dataset_manifest_path"].read_text(encoding="utf-8"))
        resolution = json.loads(inputs["annotation_resolution_path"].read_text(encoding="utf-8"))
        predictions = json.loads(inputs["predictions_path"].read_text(encoding="utf-8"))
        roles = json.loads(inputs["policy_roles_path"].read_text(encoding="utf-8"))

        rename = {"application-ball": "a-mid", "application-noise": "b-mid"}
        evaluation_ids = [
            candidate_id for role in ("policy_calibration", "policy_audit") for candidate_id in roles["roles"][role]
        ]
        for index, candidate_id in enumerate(evaluation_ids[:extra_holdouts]):
            rename[candidate_id] = f"holdout-{index:03d}"
        self._rename_candidates(
            rename,
            source_contract,
            resolved_contract,
            dataset,
            resolution,
            predictions,
            roles,
        )

        labels = list(CLASSIFICATION_LABELS)
        ball_probabilities = dict.fromkeys(labels, 0.0)
        ball_probabilities.update({"match_ball": 0.995, "equipment_or_background": 0.005})
        noise_probabilities = dict.fromkeys(labels, 0.0)
        noise_probabilities.update({"match_ball": 0.005, "equipment_or_background": 0.995})
        uncertain_probabilities = dict.fromkeys(labels, 0.0)
        uncertain_probabilities.update({"match_ball": 0.5, "equipment_or_background": 0.5})
        model_version = predictions["model_version"]

        source_candidates = {row["candidate_id"]: row for row in source_contract["candidates"]}
        resolved_candidates = {row["candidate_id"]: row for row in resolved_contract["candidates"]}
        samples = {row["candidate_id"]: row for row in dataset["samples"]}
        prediction_rows = {row["candidate_id"]: row for row in predictions["predictions"]}

        def identity(candidate_id: str, frame_index: int, bbox: list[float], confidence: float) -> dict[str, object]:
            return {
                "candidate_id": candidate_id,
                "frame_index": frame_index,
                "bbox": bbox,
                "confidence": confidence,
                "source": "detector",
            }

        def upsert_application(
            candidate_id: str,
            *,
            frame_index: int,
            bbox: list[float],
            confidence: float,
            variant_id: str,
            probabilities: dict[str, float],
        ) -> None:
            candidate = identity(candidate_id, frame_index, bbox, confidence)
            if candidate_id in source_candidates:
                source_candidates[candidate_id].update(candidate)
                resolved_candidates[candidate_id].update(candidate)
                sample = samples[candidate_id]
            else:
                source_contract["candidates"].append(candidate)
                resolved_contract["candidates"].append(deepcopy(candidate))
                source_candidates[candidate_id] = candidate
                resolved_candidates[candidate_id] = resolved_contract["candidates"][-1]
                resolved_contract["classifications"].append(
                    {
                        "candidate_id": candidate_id,
                        "label": "unknown",
                        "label_origin": "prelabel",
                        "confidence": 0.99,
                    }
                )
                resolution["resolutions"].append(
                    {
                        "candidate_id": candidate_id,
                        "status": "pending_adjudication",
                        "label": "unknown",
                        "label_origin": "prelabel",
                        "training_eligible": False,
                        "reasons": ["primary_vote_count"],
                    }
                )
                sample = {"sample_id": candidate_id, "candidate_id": candidate_id, "artifacts": {}}
                dataset["samples"].append(sample)
                samples[candidate_id] = sample
                prediction = {"candidate_id": candidate_id, "model_version": model_version}
                predictions["predictions"].append(prediction)
                prediction_rows[candidate_id] = prediction
            sample.update(
                {
                    "sample_id": candidate_id,
                    "candidate_id": candidate_id,
                    "detector_source": "detector",
                    "frame_index": frame_index,
                    "bbox_requested_pixels": bbox,
                    "bbox_clamped_pixels": bbox,
                    "bbox_normalized": [bbox[0] / 640.0, bbox[1] / 360.0, bbox[2] / 640.0, bbox[3] / 360.0],
                    "confidence": confidence,
                    "variant_id": variant_id,
                    "group_id": f"group-{variant_id}",
                    "split_group": f"split-{variant_id}",
                    "temporal_group": f"temporal-{variant_id}",
                }
            )
            prediction = prediction_rows[candidate_id]
            top_label = max(labels, key=lambda label: (probabilities[label], -labels.index(label)))
            prediction.update(
                {
                    "candidate_id": candidate_id,
                    "predicted_label": top_label,
                    "confidence": probabilities[top_label],
                    "probabilities": probabilities,
                    "model_version": model_version,
                }
            )

        if include_application_candidates:
            upsert_application(
                "a-edge",
                frame_index=0,
                bbox=[10.0, 12.0, 18.0, 20.0],
                confidence=0.7,
                variant_id="a",
                probabilities=uncertain_probabilities,
            )
            upsert_application(
                "a-mid",
                frame_index=180,
                bbox=[20.0, 22.0, 28.0, 30.0],
                confidence=0.6,
                variant_id="a",
                probabilities=ball_probabilities,
            )
            upsert_application(
                "b-mid",
                frame_index=150,
                bbox=[30.0, 32.0, 38.0, 40.0],
                confidence=0.5,
                variant_id="b",
                probabilities=noise_probabilities,
            )
        for index in range(extra_audits):
            candidate_id = f"audit-{index:03d}"
            noise = (index // 2) % 2 == 1
            upsert_application(
                candidate_id,
                frame_index=600 + index * audit_frame_step,
                bbox=[40.0, 42.0, 48.0, 50.0],
                confidence=0.65,
                variant_id="a" if index % 2 == 0 else "b",
                probabilities=noise_probabilities if noise else ball_probabilities,
            )
        for index in range(extra_uncertain):
            upsert_application(
                f"uncertain-{index:03d}",
                frame_index=600 + (extra_audits + index) * 300,
                bbox=[50.0, 52.0, 58.0, 60.0],
                confidence=0.45,
                variant_id="a" if index % 2 == 0 else "b",
                probabilities=uncertain_probabilities,
            )

        application_ids = {
            row["candidate_id"] for row in resolution["resolutions"] if row.get("status") == "pending_adjudication"
        }
        shared_evaluation_artifacts = {}
        for artifact_name, filename in (
            ("tight_tensor", "tight.npy"),
            ("context_tensor", "context.npy"),
            ("review_montage", "review_montage.png"),
        ):
            artifact = root / "evidence" / "policy-evaluation" / filename
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(f"{artifact_name}:policy-evaluation".encode())
            shared_evaluation_artifacts[artifact_name] = {
                "path": artifact.relative_to(root).as_posix(),
                "sha256": _sha256(artifact),
            }
        for candidate_id, sample in samples.items():
            if candidate_id not in application_ids:
                sample["artifacts"] = deepcopy(shared_evaluation_artifacts)
        dataset["sources"] = [
            source
            for source in dataset["sources"]
            if not any(candidate_id in application_ids for candidate_id in source["candidate_ids"])
        ]
        if application_ids:
            max_frame = max(source_candidates[candidate_id]["frame_index"] for candidate_id in application_ids)
            for variant_id in ("a", "b"):
                candidate_ids = sorted(
                    candidate_id
                    for candidate_id in application_ids
                    if samples[candidate_id]["variant_id"] == variant_id
                )
                video = root / f"{variant_id}.mp4"
                video.write_bytes((variant_id * 31).encode())
                source = {
                    "path": video.name,
                    "sha256": _sha256(video),
                    "variant_id": variant_id,
                    "width": 640,
                    "height": 360,
                    "frame_count": max(600, max_frame + 300),
                    "group_id": f"group-{variant_id}",
                    "split_group": f"split-{variant_id}",
                    "temporal_group": f"temporal-{variant_id}",
                    "candidate_ids": candidate_ids,
                }
                if fps_by_variant[variant_id] is not None:
                    source["fps"] = fps_by_variant[variant_id]
                dataset["sources"].append(source)
                for candidate_id in candidate_ids:
                    sample = samples[candidate_id]
                    artifacts = {}
                    for artifact_name, filename in (
                        ("tight_tensor", "tight.npy"),
                        ("context_tensor", "context.npy"),
                        ("review_montage", "review_montage.png"),
                    ):
                        artifact = root / "evidence" / candidate_id / filename
                        artifact.parent.mkdir(parents=True, exist_ok=True)
                        artifact.write_bytes(f"{artifact_name}:{candidate_id}".encode())
                        artifacts[artifact_name] = {
                            "path": artifact.relative_to(root).as_posix(),
                            "sha256": _sha256(artifact),
                        }
                    sample["artifacts"] = artifacts

        source_contract = build_tracking_contract(candidates=source_contract["candidates"])
        resolved_contract = build_tracking_contract(
            candidates=resolved_contract["candidates"],
            classifications=resolved_contract["classifications"],
            decisions=resolved_contract["decisions"],
        )
        _write_json(source_contract_path, source_contract)
        _write_json(inputs["resolved_contract_path"], resolved_contract)
        dataset["contract"]["sha256"] = _sha256(source_contract_path)
        dataset["frame_offsets"] = [-2, -1, 0, 1, 2]
        dataset["tensor_contract"] = {
            "color_space": "RGB",
            "dtype": "uint8",
            "tight_shape": [5, 3, 64, 64],
            "context_shape": [5, 3, 128, 128],
            "markup": False,
        }
        dataset["summary"] = {
            "status": "ok",
            "sample_count": len(dataset["samples"]),
            "source_count": len(dataset["sources"]),
        }
        dataset["sources"].sort(key=lambda row: row["variant_id"])
        dataset["samples"].sort(key=lambda row: row["candidate_id"])
        _write_json(inputs["dataset_manifest_path"], dataset)

        resolution["source_contract"]["sha256"] = _sha256(source_contract_path)
        resolution["source_dataset_manifest"]["sha256"] = _sha256(inputs["dataset_manifest_path"])
        resolution["derived_tracking_contract"]["sha256"] = _sha256(inputs["resolved_contract_path"])
        resolution["resolutions"].sort(key=lambda row: row["candidate_id"])
        _write_json(inputs["annotation_resolution_path"], resolution)

        candidate_by_id = {row["candidate_id"]: row for row in source_contract["candidates"]}
        for prediction in predictions["predictions"]:
            candidate = candidate_by_id[prediction["candidate_id"]]
            prediction["candidate_fingerprint"] = _canonical_sha256(
                {
                    "candidate_id": candidate["candidate_id"],
                    "frame_index": candidate["frame_index"],
                    "bbox": [float(value) for value in candidate["bbox"]],
                    "detector_source": candidate["source"],
                    "confidence": float(candidate["confidence"]),
                }
            )
        predictions["source_contract_sha256"] = _sha256(source_contract_path)
        predictions["prediction_count"] = len(predictions["predictions"])
        predictions["predictions"].sort(key=lambda row: row["candidate_id"])
        _write_json(inputs["predictions_path"], predictions)

        roles["roles"] = {name: sorted(candidate_ids) for name, candidate_ids in roles["roles"].items()}
        roles["candidate_component_mapping"].sort(key=lambda row: row["candidate_id"])
        roles["lineage"].update(
            {
                "predictions_sha256": _sha256(inputs["predictions_path"]),
                "dataset_manifest_sha256": _sha256(inputs["dataset_manifest_path"]),
                "annotation_resolution_sha256": _sha256(inputs["annotation_resolution_path"]),
                "resolved_contract_sha256": _sha256(inputs["resolved_contract_path"]),
            }
        )
        _write_json(inputs["policy_roles_path"], roles)

        policy_dir = root / "policy-output"
        fit_selective_policy(
            **inputs,
            output_dir=policy_dir,
            config=SelectivePolicyConfig(max_thresholds_per_lane=1),
        )
        self.root = root
        self.contract_path = source_contract_path
        self.dataset_path = inputs["dataset_manifest_path"]
        self.annotation_resolution_path = inputs["annotation_resolution_path"]
        self.resolved_contract_path = inputs["resolved_contract_path"]
        self.policy_roles_path = inputs["policy_roles_path"]
        self.model_path = inputs["model_manifest_path"]
        self.training_report_path = inputs["training_report_path"]
        self.weights_path = root / "model.pt"
        self.predictions_path = inputs["predictions_path"]
        self.policy_path = policy_dir / "selective_policy.v1.json"
        self.decisions_path = policy_dir / "selective_decisions.v1.json"
        self.dataset = dataset
        decisions = json.loads(self.decisions_path.read_text(encoding="utf-8"))
        self.application_candidate_ids = {
            row["candidate_id"] for row in decisions["decisions"] if row["decision_scope"] == "application"
        }

    @staticmethod
    def _rename_candidates(
        rename: dict[str, str],
        source_contract: dict[str, object],
        resolved_contract: dict[str, object],
        dataset: dict[str, object],
        resolution: dict[str, object],
        predictions: dict[str, object],
        roles: dict[str, object],
    ) -> None:
        def renamed(value: str) -> str:
            return rename.get(value, value)

        for contract in (source_contract, resolved_contract):
            for collection_name in ("candidates", "classifications", "decisions"):
                for row in contract.get(collection_name, []):
                    if "candidate_id" in row:
                        row["candidate_id"] = renamed(row["candidate_id"])
        for sample in dataset["samples"]:
            sample["candidate_id"] = renamed(sample["candidate_id"])
            sample["sample_id"] = renamed(sample["sample_id"])
        for source in dataset["sources"]:
            source["candidate_ids"] = [renamed(candidate_id) for candidate_id in source["candidate_ids"]]
        for row in resolution["resolutions"]:
            row["candidate_id"] = renamed(row["candidate_id"])
        for row in predictions["predictions"]:
            row["candidate_id"] = renamed(row["candidate_id"])
        for role, candidate_ids in roles["roles"].items():
            roles["roles"][role] = [renamed(candidate_id) for candidate_id in candidate_ids]
        for component in roles["components"]:
            component["candidate_ids"] = [renamed(candidate_id) for candidate_id in component["candidate_ids"]]
        for row in roles["candidate_component_mapping"]:
            row["candidate_id"] = renamed(row["candidate_id"])

    def build_queue(self, output_dir: Path, **kwargs: object) -> dict[str, object]:
        return build_selective_review_queue(
            self.dataset_path,
            self.predictions_path,
            self.policy_path,
            self.model_path,
            self.contract_path,
            output_dir,
            decisions_path=self.decisions_path,
            annotation_resolution_path=self.annotation_resolution_path,
            resolved_contract_path=self.resolved_contract_path,
            policy_roles_path=self.policy_roles_path,
            **kwargs,
        )


def _queue_action(
    queue_path: Path,
    candidate_id: str,
    action: str,
    *,
    action_id: str = "action-1",
    **extra: object,
) -> dict[str, object]:
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    item = next(item for item in queue["items"] if any(c["candidate_id"] == candidate_id for c in item["candidates"]))
    candidate = next(c for c in item["candidates"] if c["candidate_id"] == candidate_id)
    result = {
        "action_id": action_id,
        "review_item_id": item["review_item_id"],
        "candidate_id": candidate_id,
        "reviewer_id": "reviewer-1",
        "created_at": "2026-07-09T12:00:00Z",
        "action": action,
        "bindings": {
            "queue_sha256": _sha256(queue_path),
            "timing_sha256": queue["bindings"]["review_timing"]["sha256"],
            "policy_sha256": queue["bindings"]["policy"]["sha256"],
            "decisions_sha256": queue["bindings"]["decisions"]["sha256"],
            "model_sha256": queue["bindings"]["model"]["sha256"],
            "training_report_sha256": queue["bindings"]["training_report"]["sha256"],
            "model_weights_sha256": queue["bindings"]["model_weights"]["sha256"],
            "dataset_sha256": queue["bindings"]["dataset"]["sha256"],
            "predictions_sha256": queue["bindings"]["predictions"]["sha256"],
            "contract_sha256": queue["bindings"]["contract"]["sha256"],
            "annotation_resolution_sha256": queue["bindings"]["annotation_resolution"]["sha256"],
            "resolved_tracking_contract_sha256": queue["bindings"]["resolved_tracking_contract"]["sha256"],
            "policy_roles_sha256": queue["bindings"]["policy_roles"]["sha256"],
            "evidence_sha256": candidate["evidence"]["sha256"],
            "candidate_fingerprint": candidate["candidate_fingerprint"],
        },
        **extra,
    }
    for name in (
        "qualification_dataset",
        "qualification_predictions",
        "qualification_decisions",
    ):
        if name in queue["bindings"]:
            result["bindings"][f"{name}_sha256"] = queue["bindings"][name]["sha256"]
    return result


def _complete_queue_actions(queue_path: Path) -> list[dict[str, object]]:
    return [
        _queue_action(queue_path, "a-edge", "mark_unknown", action_id="complete-unknown"),
        _queue_action(queue_path, "a-mid", "confirm_ball", action_id="complete-confirm"),
        _queue_action(
            queue_path,
            "b-mid",
            "reject_noise",
            action_id="complete-reject",
            noise_subtype="equipment_or_background",
        ),
    ]


def _materialize(
    fixture: _Fixture,
    queue_path: Path,
    actions_path: Path,
    output_dir: Path,
) -> dict[str, object]:
    return materialize_selective_review_actions(
        queue_path,
        actions_path,
        fixture.dataset_path,
        fixture.predictions_path,
        fixture.policy_path,
        fixture.model_path,
        fixture.contract_path,
        output_dir,
        decisions_path=fixture.decisions_path,
        annotation_resolution_path=fixture.annotation_resolution_path,
        resolved_contract_path=fixture.resolved_contract_path,
        policy_roles_path=fixture.policy_roles_path,
    )


def _run_cli(command: list[str]) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


def _run_cli_main(function: Any, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            returncode = function(arguments)
        except SystemExit as exc:
            returncode = int(exc.code or 0)
    return subprocess.CompletedProcess(arguments, returncode, stdout.getvalue(), stderr.getvalue())


def _make_policy_inputs_reviewable(inputs: dict[str, Path]) -> None:
    dataset_path = inputs["dataset_manifest_path"]
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    dataset["frame_offsets"] = [-2, -1, 0, 1, 2]
    dataset["tensor_contract"] = {
        "color_space": "RGB",
        "dtype": "uint8",
        "tight_shape": [5, 3, 64, 64],
        "context_shape": [5, 3, 128, 128],
        "markup": False,
    }
    for source in dataset["sources"]:
        video = dataset_path.parent / source["path"]
        video.write_bytes(f"eval-video-{source['variant_id']}".encode())
        if _sha256(video) != source["sha256"]:
            raise AssertionError("policy fixture video hash preimage changed")
        source["fps"] = 20.0
    for sample in dataset["samples"]:
        artifacts = {}
        for artifact_name, filename in (
            ("tight_tensor", "tight.npy"),
            ("context_tensor", "context.npy"),
            ("review_montage", "review_montage.png"),
        ):
            artifact = dataset_path.parent / "evidence" / sample["sample_id"] / filename
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(f"{artifact_name}:{sample['sample_id']}".encode())
            artifacts[artifact_name] = {
                "path": artifact.relative_to(dataset_path.parent).as_posix(),
                "sha256": _sha256(artifact),
            }
        sample["artifacts"] = artifacts
    _write_json(dataset_path, dataset)

    resolution_path = inputs["annotation_resolution_path"]
    resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
    resolution["source_dataset_manifest"]["sha256"] = _sha256(dataset_path)
    _write_json(resolution_path, resolution)

    roles_path = inputs["policy_roles_path"]
    roles = json.loads(roles_path.read_text(encoding="utf-8"))
    roles["lineage"]["dataset_manifest_sha256"] = _sha256(dataset_path)
    roles["lineage"]["annotation_resolution_sha256"] = _sha256(resolution_path)
    _write_json(roles_path, roles)


def _integerize_policy_source_identity(inputs: dict[str, Path]) -> None:
    source_contract_path = inputs["dataset_manifest_path"].parent / "source-contract.json"
    for contract_path in (source_contract_path, inputs["resolved_contract_path"]):
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        for candidate in contract["candidates"]:
            candidate["bbox"] = [int(value) for value in candidate["bbox"]]
            candidate["confidence"] = 1
        _write_json(contract_path, contract)

    dataset = json.loads(inputs["dataset_manifest_path"].read_text(encoding="utf-8"))
    for sample in dataset["samples"]:
        sample["bbox_requested_pixels"] = [int(value) for value in sample["bbox_requested_pixels"]]
        sample["bbox_clamped_pixels"] = [int(value) for value in sample["bbox_clamped_pixels"]]
        sample["confidence"] = 1
    dataset["contract"]["sha256"] = _sha256(source_contract_path)
    _write_json(inputs["dataset_manifest_path"], dataset)

    source_candidates = {
        candidate["candidate_id"]: candidate
        for candidate in json.loads(source_contract_path.read_text(encoding="utf-8"))["candidates"]
    }
    predictions = json.loads(inputs["predictions_path"].read_text(encoding="utf-8"))
    predictions["source_contract_sha256"] = _sha256(source_contract_path)
    for prediction in predictions["predictions"]:
        candidate = source_candidates[prediction["candidate_id"]]
        prediction["candidate_fingerprint"] = _canonical_sha256(
            {
                "candidate_id": candidate["candidate_id"],
                "frame_index": candidate["frame_index"],
                "bbox": [float(value) for value in candidate["bbox"]],
                "detector_source": candidate["source"],
                "confidence": float(candidate["confidence"]),
            }
        )
    _write_json(inputs["predictions_path"], predictions)

    resolution = json.loads(inputs["annotation_resolution_path"].read_text(encoding="utf-8"))
    resolution["source_contract"]["sha256"] = _sha256(source_contract_path)
    resolution["source_dataset_manifest"]["sha256"] = _sha256(inputs["dataset_manifest_path"])
    resolution["derived_tracking_contract"]["sha256"] = _sha256(inputs["resolved_contract_path"])
    _write_json(inputs["annotation_resolution_path"], resolution)

    roles = json.loads(inputs["policy_roles_path"].read_text(encoding="utf-8"))
    roles["lineage"].update(
        {
            "predictions_sha256": _sha256(inputs["predictions_path"]),
            "dataset_manifest_sha256": _sha256(inputs["dataset_manifest_path"]),
            "annotation_resolution_sha256": _sha256(inputs["annotation_resolution_path"]),
            "resolved_contract_sha256": _sha256(inputs["resolved_contract_path"]),
        }
    )
    _write_json(inputs["policy_roles_path"], roles)


class SelectiveReviewWindowTests(unittest.TestCase):
    def test_selection_report_grouping_scales_linearly_with_population(self) -> None:
        class CountingRow(dict[str, Any]):
            accesses = 0

            def __getitem__(self, key: str) -> Any:
                type(self).accesses += 1
                return super().__getitem__(key)

        eligible: list[dict[str, Any]] = [
            CountingRow(
                candidate_id=f"candidate-{index:04d}",
                selective_decision="accept" if index % 2 == 0 else "reject",
                review_kind="audit_accept" if index % 2 == 0 else "audit_reject",
                variant_id=f"variant-{index:04d}",
            )
            for index in range(1000)
        ]
        selected = eligible[::2]
        CountingRow.accesses = 0

        report = _selection_report(eligible, selected, max_windows=30, mandatory_window_count=0)

        self.assertEqual(1000, len(report["by_variant"]))
        self.assertEqual(500, report["counts"]["selected"])
        self.assertLess(CountingRow.accesses, 20_000)

    def test_windows_are_fps_aware_edge_shifted_merged_and_split(self) -> None:
        timings = {
            "20": {"fps": 20.0, "frame_count": 400},
            "ntsc": {"fps": 29.97, "frame_count": 900},
        }
        windows = build_review_windows(
            [
                {"candidate_id": "edge", "variant_id": "20", "frame_index": 0, "review_kind": "uncertainty"},
                {"candidate_id": "m1", "variant_id": "ntsc", "frame_index": 300, "review_kind": "uncertainty"},
                {"candidate_id": "m2", "variant_id": "ntsc", "frame_index": 330, "review_kind": "conflict"},
                {"candidate_id": "split", "variant_id": "ntsc", "frame_index": 525, "review_kind": "audit_accept"},
            ],
            timings,
            window_seconds=7.5,
        )
        edge = next(item for item in windows if item["variant_id"] == "20")
        self.assertEqual((0, 149), (edge["start_frame"], edge["end_frame"]))
        self.assertTrue(math.isclose(edge["duration_seconds"], 7.5))
        ntsc = [item for item in windows if item["variant_id"] == "ntsc"]
        self.assertEqual(2, len(ntsc))
        self.assertLessEqual(max(item["duration_seconds"] for item in ntsc), 10.0)
        self.assertEqual({"m1", "m2"}, {row["candidate_id"] for row in ntsc[0]["candidates"]})

    def test_short_source_fails_closed(self) -> None:
        with self.assertRaisesRegex(SelectiveReviewError, "shorter than the required 5-second"):
            build_review_windows(
                [{"candidate_id": "short", "variant_id": "v", "frame_index": 10, "review_kind": "uncertainty"}],
                {"v": {"fps": 20.0, "frame_count": 80}},
            )

    def test_hard_max_thirty_windows(self) -> None:
        candidates = [
            {"candidate_id": f"c{i:02d}", "variant_id": "v", "frame_index": i * 300, "review_kind": "uncertainty"}
            for i in range(31)
        ]
        with self.assertRaisesRegex(SelectiveReviewError, "30"):
            build_review_windows(candidates, {"v": {"fps": 20.0, "frame_count": 10000}})

    def test_fractional_fps_rounding_stays_inside_duration_contract(self) -> None:
        timing = {"v": {"fps": 29.97, "frame_count": 900}}
        for requested in (5.0, 10.0):
            windows = build_review_windows(
                [{"candidate_id": str(requested), "variant_id": "v", "frame_index": 450, "review_kind": "uncertainty"}],
                timing,
                window_seconds=requested,
            )
            self.assertGreaterEqual(windows[0]["duration_seconds"], 5.0)
            self.assertLessEqual(windows[0]["duration_seconds"], 10.0)
        with self.assertRaisesRegex(SelectiveReviewError, "cannot represent"):
            build_review_windows(
                [{"candidate_id": "slow", "variant_id": "v", "frame_index": 0, "review_kind": "uncertainty"}],
                {"v": {"fps": 0.05, "frame_count": 2}},
            )

    def test_dense_audit_selection_builds_windows_a_constant_number_of_times(self) -> None:
        candidates = [
            {
                "candidate_id": f"dense-{index:04d}",
                "candidate_fingerprint": f"fingerprint-{index:04d}",
                "variant_id": "v",
                "frame_index": 200 + index % 50,
                "selective_decision": "accept" if index % 2 == 0 else "reject",
                "review_kind": "audit_accept" if index % 2 == 0 else "audit_reject",
            }
            for index in range(3000)
        ]
        timings = {"v": {"fps": 20.0, "frame_count": 1000}}
        with patch(
            "football_tracking.selective_review.build_review_windows",
            wraps=build_review_windows,
        ) as window_builder:
            selected, windows, selection = _select_review_candidates(
                candidates,
                timings,
                window_seconds=7.5,
                max_windows=30,
            )
        self.assertEqual(3000, len(selected))
        self.assertEqual(3000, selection["counts"]["selected"])
        self.assertLessEqual(len(windows), 30)
        self.assertLessEqual(window_builder.call_count, 2)

    def test_mixed_dense_sparse_audits_use_incremental_window_budgeting(self) -> None:
        candidates = []
        for index in range(3000):
            dense = index < 2400
            decision = "accept" if index % 2 == 0 else "reject"
            candidates.append(
                {
                    "candidate_id": f"mixed-{index:04d}",
                    "candidate_fingerprint": f"fingerprint-{index:04d}",
                    "variant_id": "v",
                    "frame_index": 200 + index % 50 if dense else 1000 + (index - 2400) * 300,
                    "selective_decision": decision,
                    "review_kind": "audit_accept" if decision == "accept" else "audit_reject",
                }
            )
        timings = {"v": {"fps": 20.0, "frame_count": 200000}}
        with patch(
            "football_tracking.selective_review.build_review_windows",
            wraps=build_review_windows,
        ) as window_builder:
            selected, windows, selection = _select_review_candidates(
                candidates,
                timings,
                window_seconds=7.5,
                max_windows=30,
            )
        self.assertLess(len(selected), len(candidates))
        self.assertEqual(len(selected), selection["counts"]["selected"])
        self.assertLessEqual(len(windows), 30)
        self.assertLessEqual(window_builder.call_count, 3)

    def test_incremental_budget_rescues_candidates_after_exact_repartitioning(self) -> None:
        raw = [
            ("c9", "4219eff5357b5d30", 333, "reject"),
            ("c11", "6f315ecb2c377607", 664, "reject"),
            ("c12", "4152be29fa6aaaa2", 1054, "accept"),
            ("c13", "ea2c3d6b45faf2db", 1248, "reject"),
            ("c16", "e583188aeb870fbf", 858, "accept"),
            ("c17", "93ee56211f9ef530", 1740, "reject"),
            ("c18", "34f9c8c3a5a38630", 2374, "accept"),
        ]
        candidates = [
            {
                "candidate_id": candidate_id,
                "candidate_fingerprint": fingerprint,
                "variant_id": "v",
                "frame_index": frame_index,
                "selective_decision": decision,
                "review_kind": "audit_accept" if decision == "accept" else "audit_reject",
            }
            for candidate_id, fingerprint, frame_index, decision in raw
        ]
        selected, windows, selection = _select_review_candidates(
            candidates,
            {"v": {"fps": 59.94, "frame_count": 2500}},
            window_seconds=5.5,
            max_windows=4,
        )
        selected_ids = {candidate["candidate_id"] for candidate in selected}
        self.assertIn("c11", selected_ids)
        self.assertNotIn("c17", selected_ids)
        self.assertEqual(4, len(windows))
        self.assertEqual(["c17"], selection["dropped_candidate_ids"])


class SelectiveReviewArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        inference_patch = patch(
            "football_tracking.selective_policy.validate_candidate_predictions_package"
        )
        inference_patch.start()
        self.addCleanup(inference_patch.stop)

    def test_target_application_queue_carries_qualification_bindings_through_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            application_path = root / "selective_policy_application.v1.json"
            application = json.loads(fixture.decisions_path.read_text(encoding="utf-8"))
            application["artifact_type"] = "selective_policy_application"
            _write_json(application_path, application)

            with patch(
                "football_tracking.selective_review.validate_selective_policy_application_binding",
                return_value={"application": application},
            ):
                queue_dir = root / "target-queue"
                queue = build_selective_review_queue(
                    fixture.dataset_path,
                    fixture.predictions_path,
                    fixture.policy_path,
                    fixture.model_path,
                    fixture.contract_path,
                    queue_dir,
                    decisions_path=application_path,
                    annotation_resolution_path=fixture.annotation_resolution_path,
                    resolved_contract_path=fixture.resolved_contract_path,
                    policy_roles_path=fixture.policy_roles_path,
                    qualification_dataset_manifest_path=fixture.dataset_path,
                    qualification_predictions_path=fixture.predictions_path,
                    qualification_decisions_path=fixture.decisions_path,
                )
                self.assertTrue(
                    {
                        "qualification_dataset",
                        "qualification_predictions",
                        "qualification_decisions",
                    }.issubset(queue["bindings"])
                )
                queue_path = queue_dir / "selective_review_queue.v1.json"
                actions_path = root / "target-actions.json"
                _write_json(
                    actions_path,
                    {
                        "schema_version": "1.0",
                        "artifact_type": "selective_review_actions",
                        "actions": _complete_queue_actions(queue_path),
                    },
                )
                report = materialize_selective_review_actions(
                    queue_path,
                    actions_path,
                    fixture.dataset_path,
                    fixture.predictions_path,
                    fixture.policy_path,
                    fixture.model_path,
                    fixture.contract_path,
                    root / "target-round",
                    decisions_path=application_path,
                    annotation_resolution_path=fixture.annotation_resolution_path,
                    resolved_contract_path=fixture.resolved_contract_path,
                    policy_roles_path=fixture.policy_roles_path,
                    qualification_dataset_manifest_path=fixture.dataset_path,
                    qualification_predictions_path=fixture.predictions_path,
                    qualification_decisions_path=fixture.decisions_path,
                )

            self.assertEqual("complete", report["status"])

    def test_large_audit_population_is_deterministically_bounded_and_fair(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(
                root,
                fps_by_variant={"a": 20.0, "b": 20.0},
                extra_audits=100,
            )
            first = fixture.build_queue(root / "queue-1")
            second = fixture.build_queue(root / "queue-2")
            self.assertLessEqual(first["review_item_count"], 30)
            self.assertEqual(first["items"], second["items"])
            self.assertEqual(first["selection"], second["selection"])
            selection = first["selection"]
            self.assertEqual(len(fixture.application_candidate_ids), selection["counts"]["eligible"])
            self.assertEqual(first["candidate_count"], selection["counts"]["selected"])
            self.assertGreater(selection["counts"]["dropped"], 0)
            self.assertFalse(selection["coverage_complete"])
            self.assertTrue(selection["requires_additional_round"])
            self.assertTrue(selection["by_kind"])
            self.assertTrue(selection["by_variant"])
            audit_strata = [
                row["selected"]
                for row in selection["by_decision_variant"]
                if row["decision"] in {"accept", "reject"} and row["eligible"]
            ]
            self.assertLessEqual(max(audit_strata) - min(audit_strata), 1)

    def test_dense_audit_population_uses_actual_window_budget_and_is_fully_covered(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(
                root,
                fps_by_variant={"a": 20.0, "b": 20.0},
                extra_audits=100,
                audit_frame_step=1,
            )
            queue = fixture.build_queue(root / "queue")
            self.assertLessEqual(queue["review_item_count"], 10)
            self.assertEqual(len(fixture.application_candidate_ids), queue["candidate_count"])
            self.assertEqual(0, queue["selection"]["counts"]["dropped"])
            self.assertTrue(queue["selection"]["coverage_complete"])
            self.assertFalse(queue["selection"]["requires_additional_round"])

    def test_more_than_thirty_mandatory_reviews_requires_narrower_scope(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(
                root,
                fps_by_variant={"a": 20.0, "b": 20.0},
                extra_uncertain=31,
            )
            with self.assertRaisesRegex(SelectiveReviewError, "uncertainty/conflict.*30.*narrow"):
                fixture.build_queue(root / "queue")

    def test_evaluation_holdouts_are_validated_but_excluded_from_queue_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(
                root,
                fps_by_variant={"a": 20.0, "b": 20.0},
                extra_holdouts=31,
            )
            queue = fixture.build_queue(root / "queue")
            selected_ids = {candidate["candidate_id"] for item in queue["items"] for candidate in item["candidates"]}
            self.assertEqual({"a-edge", "a-mid", "b-mid"}, selected_ids)
            self.assertEqual(3, queue["selection"]["counts"]["eligible"])
            self.assertEqual(0, queue["selection"]["counts"]["dropped"])
            self.assertTrue(queue["selection"]["coverage_complete"])

    def test_evaluation_holdout_invariants_fail_closed(self) -> None:
        mutations = {
            "decision": lambda row: row.__setitem__("decision", "accept"),
            "applied": lambda row: row.__setitem__("applied_to_contract", True),
            "role": lambda row: row.__setitem__("policy_role", "application"),
            "reason": lambda row: row.__setitem__("forced_abstain_reasons", []),
            "scope": lambda row: row.__setitem__("decision_scope", "unknown"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                fixture = _Fixture(
                    root,
                    fps_by_variant={"a": 20.0, "b": 20.0},
                    extra_holdouts=1,
                )
                decisions = json.loads(fixture.decisions_path.read_text(encoding="utf-8"))
                holdout = next(row for row in decisions["decisions"] if row["candidate_id"].startswith("holdout-"))
                mutate(holdout)
                _write_json(fixture.decisions_path, decisions)
                _seal_policy_decisions(fixture.policy_path, fixture.decisions_path)
                with self.assertRaisesRegex(
                    SelectiveReviewError, "scope|evaluation holdout|evaluation cohort|non-cohort|population"
                ):
                    fixture.build_queue(root / "queue")

    def test_official_policy_fit_output_excludes_holdouts_from_queue(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inputs = _write_inputs(
                root,
                calibration_per_class=8,
                audit_per_class=8,
                include_application_cases=True,
            )
            _make_policy_inputs_reviewable(inputs)
            policy_dir = root / "policy-output"
            fit_selective_policy(**inputs, output_dir=policy_dir)
            queue = build_selective_review_queue(
                inputs["dataset_manifest_path"],
                inputs["predictions_path"],
                policy_dir / "selective_policy.v1.json",
                inputs["model_manifest_path"],
                root / "source-contract.json",
                root / "queue",
                decisions_path=policy_dir / "selective_decisions.v1.json",
                annotation_resolution_path=inputs["annotation_resolution_path"],
                resolved_contract_path=inputs["resolved_contract_path"],
                policy_roles_path=inputs["policy_roles_path"],
            )
            selected_ids = {candidate["candidate_id"] for item in queue["items"] for candidate in item["candidates"]}
            self.assertEqual({"application-ball", "application-noise"}, selected_ids)
            self.assertEqual(2, queue["selection"]["counts"]["eligible"])
            self.assertEqual(0, queue["selection"]["counts"]["dropped"])

    def test_official_fit_to_queue_normalizes_integer_candidate_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inputs = _write_inputs(
                root,
                calibration_per_class=8,
                audit_per_class=8,
                include_application_cases=True,
            )
            _make_policy_inputs_reviewable(inputs)
            _integerize_policy_source_identity(inputs)
            source_contract_path = root / "source-contract.json"
            source_contract = json.loads(source_contract_path.read_text(encoding="utf-8"))
            self.assertIsInstance(source_contract["candidates"][0]["bbox"][0], int)
            self.assertIsInstance(source_contract["candidates"][0]["confidence"], int)

            policy_dir = root / "policy-output"
            fit_selective_policy(**inputs, output_dir=policy_dir)
            queue = build_selective_review_queue(
                inputs["dataset_manifest_path"],
                inputs["predictions_path"],
                policy_dir / "selective_policy.v1.json",
                inputs["model_manifest_path"],
                source_contract_path,
                root / "queue",
                decisions_path=policy_dir / "selective_decisions.v1.json",
                annotation_resolution_path=inputs["annotation_resolution_path"],
                resolved_contract_path=inputs["resolved_contract_path"],
                policy_roles_path=inputs["policy_roles_path"],
            )

            selected_ids = {candidate["candidate_id"] for item in queue["items"] for candidate in item["candidates"]}
            self.assertEqual({"application-ball", "application-noise"}, selected_ids)

    def test_independent_decisions_and_matching_lineage_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            with self.assertRaisesRegex(SelectiveReviewError, "independent selective decisions"):
                build_selective_review_queue(
                    fixture.dataset_path,
                    fixture.predictions_path,
                    fixture.policy_path,
                    fixture.model_path,
                    fixture.contract_path,
                    root / "missing-decisions",
                )
            policy = json.loads(fixture.policy_path.read_text(encoding="utf-8"))
            decisions = json.loads(fixture.decisions_path.read_text(encoding="utf-8"))
            policy.pop("lineage")
            decisions.pop("lineage")
            _write_json(fixture.decisions_path, decisions)
            _seal_policy_decisions(fixture.policy_path, fixture.decisions_path, policy=policy)
            with self.assertRaisesRegex(SelectiveReviewError, "lineage"):
                fixture.build_queue(root / "missing-lineage")

    def test_policy_binds_exact_decisions_snapshot_content_and_version_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            fixture.build_queue(root / "baseline")
            decisions = json.loads(fixture.decisions_path.read_text(encoding="utf-8"))
            row = next(item for item in decisions["decisions"] if item["candidate_id"] == "a-mid")
            row["decision"] = "reject"
            _write_json(fixture.decisions_path, decisions)
            with self.assertRaisesRegex(SelectiveReviewError, "decisions.*artifact.*sha256|content sha256"):
                fixture.build_queue(root / "tampered-decisions")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            policy = json.loads(fixture.policy_path.read_text(encoding="utf-8"))
            policy["version_inputs"]["status"] = "tampered"
            _write_json(fixture.policy_path, policy)
            with self.assertRaisesRegex(SelectiveReviewError, "version_inputs"):
                fixture.build_queue(root / "tampered-version")

    def test_queue_rejects_resealed_self_consistent_decision_forgery(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            fixture.build_queue(root / "baseline")
            valid_policy = json.loads(fixture.policy_path.read_text(encoding="utf-8"))
            valid_decisions = json.loads(fixture.decisions_path.read_text(encoding="utf-8"))

            def score_flip(decisions: dict[str, object]) -> None:
                row = next(item for item in decisions["decisions"] if item["candidate_id"] == "b-mid")
                row.update(
                    {
                        "accept_score": 0.995,
                        "reject_score": 0.005,
                        "unknown_score": 0.0,
                        "top_label": "match_ball",
                        "top_margin": 0.99,
                        "raw_decision": "accept",
                        "decision": "accept",
                    }
                )

            def forge_existing_decision(decisions: dict[str, object]) -> None:
                row = next(item for item in decisions["decisions"] if item["candidate_id"] == "a-mid")
                row.update(
                    {
                        "raw_decision": "abstain",
                        "decision": "abstain",
                        "forced_abstain_reasons": ["existing_decision"],
                        "existing_decision_preserved": True,
                        "applied_to_contract": False,
                    }
                )

            for name, mutate in {
                "score-flip": score_flip,
                "forged-existing-decision": forge_existing_decision,
            }.items():
                with self.subTest(name=name):
                    policy = deepcopy(valid_policy)
                    decisions = deepcopy(valid_decisions)
                    mutate(decisions)
                    _write_json(fixture.decisions_path, decisions)
                    _reseal_policy_decisions(fixture.policy_path, fixture.decisions_path, policy)
                    with self.assertRaisesRegex(
                        SelectiveReviewError,
                        "qualification evidence.*authoritative evidence.*resolved contract",
                    ):
                        fixture.build_queue(root / f"forged-{name}")

    def test_queue_rejects_resealed_qualified_policy_without_required_evidence(self) -> None:
        def calibration_components_below_minimum(policy: dict[str, object]) -> None:
            policy["inferential_unit"]["calibration_component_count"] = 2
            policy["calibration"]["calibration_count"] = 2
            policy["calibration"]["calibration_component_count"] = 2
            policy["calibration"]["independent_component_gate"] = {
                "observed": 2,
                "minimum": 3,
                "passed": False,
            }

        def audit_accepted_below_minimum(policy: dict[str, object]) -> None:
            policy["audit"]["one_sided_confidence"]["accepted_component_count"] = 99
            policy["audit"]["sample_gates"]["accepted_components"] = {
                "observed": 99,
                "minimum": 100,
                "passed": False,
            }

        def audit_components_below_minimum(policy: dict[str, object]) -> None:
            policy["audit"]["audit_component_count"] = 2
            policy["audit"]["one_sided_confidence"]["accepted_component_count"] = 2
            policy["audit"]["one_sided_confidence"]["true_ball_component_count"] = 2
            policy["audit"]["sample_gates"] = {
                "accepted_components": {"observed": 2, "minimum": 100, "passed": False},
                "true_ball_components": {"observed": 2, "minimum": 300, "passed": False},
                "independent_components": {"observed": 2, "minimum": 3, "passed": False},
            }
            policy["inferential_unit"]["audit_component_count"] = 2

        def audit_true_balls_below_minimum(policy: dict[str, object]) -> None:
            policy["audit"]["one_sided_confidence"]["true_ball_component_count"] = 299
            policy["audit"]["sample_gates"]["true_ball_components"] = {
                "observed": 299,
                "minimum": 300,
                "passed": False,
            }

        def point_targets_failed(policy: dict[str, object]) -> None:
            policy["audit"]["point_targets_passed"] = False

        def exact_confidence_failed(policy: dict[str, object]) -> None:
            policy["audit"]["one_sided_confidence"]["passed"] = False

        mutations = {
            "calibration-components": calibration_components_below_minimum,
            "audit-components": audit_components_below_minimum,
            "audit-accepted": audit_accepted_below_minimum,
            "audit-true-balls": audit_true_balls_below_minimum,
            "point-targets": point_targets_failed,
            "exact-confidence": exact_confidence_failed,
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            valid_policy = json.loads(fixture.policy_path.read_text(encoding="utf-8"))
            valid_decisions = json.loads(fixture.decisions_path.read_text(encoding="utf-8"))
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    policy = deepcopy(valid_policy)
                    _write_json(fixture.decisions_path, valid_decisions)
                    mutate(policy)
                    _reseal_policy_decisions(fixture.policy_path, fixture.decisions_path, policy)

                    with self.assertRaisesRegex(SelectiveReviewError, "calibration|audit|qualification|cohort"):
                        fixture.build_queue(root / f"queue-{name}")

    def test_queue_rejects_resealed_fake_calibration_and_audit_payloads(self) -> None:
        def selected_hypothesis_empty(policy: dict[str, object]) -> None:
            policy["calibration"]["selected_hypothesis"] = {}

        def certified_pair_bad_id(policy: dict[str, object]) -> None:
            policy["calibration"]["certified_pairs"][0]["accept_hypothesis_id"] = "accept-not-rejected"

        def certified_pair_bad_threshold(policy: dict[str, object]) -> None:
            policy["calibration"]["certified_pairs"][0]["accept_threshold"] = 0.123

        def fake_hypothesis_p_value(policy: dict[str, object]) -> None:
            policy["calibration"]["accept_hypotheses"][0]["p_value"] = 0.0

        def fake_holm_set(policy: dict[str, object]) -> None:
            policy["calibration"]["holm_rejected_hypotheses"] = []

        def minimal_audit(policy: dict[str, object]) -> None:
            policy["audit"] = {}

        def wrong_exact_bound(policy: dict[str, object]) -> None:
            policy["audit"]["one_sided_confidence"]["accept_error_exact_upper"] = 0.0

        def wrong_benchmark_error(policy: dict[str, object]) -> None:
            policy["audit"]["benchmark"]["candidate_evaluations"][0]["truth"] = "noise"

        def wrong_sample_gate(policy: dict[str, object]) -> None:
            policy["audit"]["sample_gates"]["accepted_components"]["passed"] = False

        mutations = {
            "selected-empty": selected_hypothesis_empty,
            "pair-bad-id": certified_pair_bad_id,
            "pair-bad-threshold": certified_pair_bad_threshold,
            "fake-p-value": fake_hypothesis_p_value,
            "fake-holm": fake_holm_set,
            "minimal-audit": minimal_audit,
            "wrong-exact-bound": wrong_exact_bound,
            "wrong-benchmark-error": wrong_benchmark_error,
            "wrong-sample-gate": wrong_sample_gate,
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            valid_policy = json.loads(fixture.policy_path.read_text(encoding="utf-8"))
            valid_decisions = json.loads(fixture.decisions_path.read_text(encoding="utf-8"))
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    policy = deepcopy(valid_policy)
                    _write_json(fixture.decisions_path, valid_decisions)
                    mutate(policy)
                    _reseal_policy_decisions(fixture.policy_path, fixture.decisions_path, policy)

                    with self.assertRaisesRegex(SelectivePolicyError, "calibration|audit|hypothesis|Holm|pair"):
                        validate_selective_decisions_binding(fixture.policy_path, fixture.decisions_path)
                    with self.assertRaisesRegex(SelectiveReviewError, "calibration|audit|hypothesis|Holm|pair"):
                        fixture.build_queue(root / f"queue-{name}")

    def test_queue_requires_exact_version_bound_evaluation_cohort_partition(self) -> None:
        def bogus_audit_benchmark_id(policy: dict[str, object], decisions: dict[str, object]) -> None:
            del decisions
            policy["audit"]["benchmark"]["candidate_evaluations"][0]["candidate_id"] = "bogus-audit-id"

        def missing_cohort_id(policy: dict[str, object], decisions: dict[str, object]) -> None:
            del decisions
            policy["evaluation_cohorts"]["calibration_candidate_ids"].pop()

        def extra_cohort_id(policy: dict[str, object], decisions: dict[str, object]) -> None:
            del decisions
            policy["evaluation_cohorts"]["audit_candidate_ids"].append("extra-audit-id")

        def duplicate_cohort_id(policy: dict[str, object], decisions: dict[str, object]) -> None:
            del decisions
            candidate_ids = policy["evaluation_cohorts"]["audit_candidate_ids"]
            candidate_ids.append(candidate_ids[0])
            candidate_ids.sort()

        def cohort_role_mismatch(policy: dict[str, object], decisions: dict[str, object]) -> None:
            candidate_id = policy["evaluation_cohorts"]["calibration_candidate_ids"][0]
            row = next(row for row in decisions["decisions"] if row["candidate_id"] == candidate_id)
            row["policy_role"] = "policy_audit"

        def cohort_scope_mismatch(policy: dict[str, object], decisions: dict[str, object]) -> None:
            candidate_id = policy["evaluation_cohorts"]["audit_candidate_ids"][0]
            row = next(row for row in decisions["decisions"] if row["candidate_id"] == candidate_id)
            row["decision_scope"] = "application"

        def missing_cohort_decision(policy: dict[str, object], decisions: dict[str, object]) -> None:
            candidate_id = policy["evaluation_cohorts"]["calibration_candidate_ids"][0]
            decisions["decisions"] = [row for row in decisions["decisions"] if row["candidate_id"] != candidate_id]

        def duplicate_cohort_decision(policy: dict[str, object], decisions: dict[str, object]) -> None:
            candidate_id = policy["evaluation_cohorts"]["audit_candidate_ids"][0]
            row = next(row for row in decisions["decisions"] if row["candidate_id"] == candidate_id)
            decisions["decisions"].append(deepcopy(row))

        def noncohort_evaluation_scope(policy: dict[str, object], decisions: dict[str, object]) -> None:
            cohort_ids = {
                *policy["evaluation_cohorts"]["calibration_candidate_ids"],
                *policy["evaluation_cohorts"]["audit_candidate_ids"],
            }
            row = next(row for row in decisions["decisions"] if row["candidate_id"] not in cohort_ids)
            row["decision_scope"] = "evaluation_only"

        def delete_application_decision(policy: dict[str, object], decisions: dict[str, object]) -> None:
            candidate_id = policy["evaluation_cohorts"]["application_candidate_ids"][0]
            decisions["decisions"] = [row for row in decisions["decisions"] if row["candidate_id"] != candidate_id]

        def forge_application_decision(policy: dict[str, object], decisions: dict[str, object]) -> None:
            candidate_id = policy["evaluation_cohorts"]["application_candidate_ids"][0]
            row = deepcopy(next(row for row in decisions["decisions"] if row["candidate_id"] == candidate_id))
            row["candidate_id"] = "forged-application-id"
            decisions["decisions"].append(row)

        def calibration_application_swap(policy: dict[str, object], decisions: dict[str, object]) -> None:
            calibration_ids = policy["evaluation_cohorts"]["calibration_candidate_ids"]
            application_ids = policy["evaluation_cohorts"]["application_candidate_ids"]
            calibration_id = calibration_ids[0]
            application_id = application_ids[0]
            calibration_ids[0] = application_id
            application_ids[0] = calibration_id
            calibration_ids.sort()
            application_ids.sort()
            calibration_row = next(row for row in decisions["decisions"] if row["candidate_id"] == calibration_id)
            calibration_row.update(
                {
                    "policy_role": None,
                    "decision_scope": "application",
                    "forced_abstain_reasons": [],
                }
            )
            application_row = next(row for row in decisions["decisions"] if row["candidate_id"] == application_id)
            application_row.update(
                {
                    "policy_role": "policy_calibration",
                    "decision_scope": "evaluation_only",
                    "decision": "abstain",
                    "applied_to_contract": False,
                    "forced_abstain_reasons": ["evaluation_holdout"],
                }
            )

        def unsorted_application_cohort(policy: dict[str, object], decisions: dict[str, object]) -> None:
            del decisions
            policy["evaluation_cohorts"]["application_candidate_ids"].reverse()

        def duplicate_application_cohort(policy: dict[str, object], decisions: dict[str, object]) -> None:
            del decisions
            candidate_ids = policy["evaluation_cohorts"]["application_candidate_ids"]
            candidate_ids.append(candidate_ids[0])
            candidate_ids.sort()

        def overlapping_application_cohort(policy: dict[str, object], decisions: dict[str, object]) -> None:
            del decisions
            candidate_ids = policy["evaluation_cohorts"]["application_candidate_ids"]
            candidate_ids.append(policy["evaluation_cohorts"]["calibration_candidate_ids"][0])
            candidate_ids.sort()

        mutations = {
            "bogus-audit-id": bogus_audit_benchmark_id,
            "missing-cohort-id": missing_cohort_id,
            "extra-cohort-id": extra_cohort_id,
            "duplicate-cohort-id": duplicate_cohort_id,
            "role-mismatch": cohort_role_mismatch,
            "scope-mismatch": cohort_scope_mismatch,
            "missing-decision": missing_cohort_decision,
            "duplicate-decision": duplicate_cohort_decision,
            "noncohort-evaluation": noncohort_evaluation_scope,
            "delete-application": delete_application_decision,
            "forge-application": forge_application_decision,
            "calibration-application-swap": calibration_application_swap,
            "unsorted-application-cohort": unsorted_application_cohort,
            "duplicate-application-cohort": duplicate_application_cohort,
            "overlapping-application-cohort": overlapping_application_cohort,
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            valid_policy = json.loads(fixture.policy_path.read_text(encoding="utf-8"))
            valid_decisions = json.loads(fixture.decisions_path.read_text(encoding="utf-8"))
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    policy = deepcopy(valid_policy)
                    decisions = deepcopy(valid_decisions)
                    mutate(policy, decisions)
                    _write_json(fixture.decisions_path, decisions)
                    _reseal_policy_decisions(fixture.policy_path, fixture.decisions_path, policy)

                    expected_error = "cohort|candidate_ids|decision|audit benchmark|population"
                    with self.assertRaisesRegex(SelectivePolicyError, expected_error):
                        validate_selective_decisions_binding(fixture.policy_path, fixture.decisions_path)
                    with self.assertRaisesRegex(SelectiveReviewError, expected_error):
                        fixture.build_queue(root / f"queue-{name}")

    def test_external_evidence_rejects_fully_resealed_application_cohort_swaps(self) -> None:
        def replace_candidate_ids(value: object, old: str, new: str) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "candidate_id" and item == old:
                        value[key] = new
                    else:
                        replace_candidate_ids(item, old, new)
            elif isinstance(value, list):
                for item in value:
                    replace_candidate_ids(item, old, new)

        def coordinated_swap(policy: dict[str, object], decisions: dict[str, object], role: str) -> None:
            cohort_name = "calibration_candidate_ids" if role == "policy_calibration" else "audit_candidate_ids"
            cohort_ids = policy["evaluation_cohorts"][cohort_name]
            application_ids = policy["evaluation_cohorts"]["application_candidate_ids"]
            application_id = "a-mid"
            evaluation_id = next(candidate_id for candidate_id in cohort_ids if "-ball-" in candidate_id)
            cohort_ids[cohort_ids.index(evaluation_id)] = application_id
            cohort_ids.sort()
            application_ids[application_ids.index(application_id)] = evaluation_id
            application_ids.sort()
            if role == "policy_calibration":
                calibration_ids = policy["calibration"]["calibration_candidate_ids"]
                calibration_ids[calibration_ids.index(evaluation_id)] = application_id
                calibration_ids.sort()
            else:
                replace_candidate_ids(policy["audit"], evaluation_id, application_id)

            evaluation_row = next(row for row in decisions["decisions"] if row["candidate_id"] == evaluation_id)
            application_row = next(row for row in decisions["decisions"] if row["candidate_id"] == application_id)
            application_row.update(
                {
                    "raw_decision": "abstain",
                    "decision": "abstain",
                    "decision_scope": "evaluation_only",
                    "policy_role": role,
                    "forced_abstain_reasons": ["evaluation_holdout"],
                    "applied_to_contract": False,
                }
            )
            evaluation_row.update(
                {
                    "raw_decision": "accept",
                    "decision": "accept",
                    "decision_scope": "application",
                    "policy_role": None,
                    "forced_abstain_reasons": [],
                    "applied_to_contract": True,
                }
            )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            valid_policy = json.loads(fixture.policy_path.read_text(encoding="utf-8"))
            valid_decisions = json.loads(fixture.decisions_path.read_text(encoding="utf-8"))
            for role in ("policy_calibration", "policy_audit"):
                with self.subTest(role=role):
                    policy = deepcopy(valid_policy)
                    decisions = deepcopy(valid_decisions)
                    coordinated_swap(policy, decisions, role)
                    _write_json(fixture.decisions_path, decisions)
                    _reseal_policy_decisions(fixture.policy_path, fixture.decisions_path, policy)

                    validate_selective_decisions_binding(fixture.policy_path, fixture.decisions_path)
                    with self.assertRaisesRegex(
                        SelectiveReviewError,
                        "qualification evidence|human-confirmed|policy role|evaluation cohort",
                    ):
                        fixture.build_queue(root / f"queue-{role}")

    def test_minimal_self_hashed_policy_is_rejected_at_review_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            policy = json.loads(fixture.policy_path.read_text(encoding="utf-8"))
            decisions = json.loads(fixture.decisions_path.read_text(encoding="utf-8"))
            content_sha256 = _decisions_content_sha256(decisions)
            version_inputs = {
                "schema_version": "1.0",
                "qualification": {"policy_status": "qualified"},
                "decisions_content_sha256": content_sha256,
            }
            policy_version = _canonical_sha256(version_inputs)
            decisions["policy_version"] = policy_version
            _write_json(fixture.decisions_path, decisions)
            policy["policy_version"] = policy_version
            policy["version_inputs"] = version_inputs
            policy["decisions_artifact"] = {
                "path": "selective_decisions.v1.json",
                "sha256": _sha256(fixture.decisions_path),
                "content_sha256": content_sha256,
            }
            _write_json(fixture.policy_path, policy)

            with self.assertRaisesRegex(SelectiveReviewError, "version_inputs.*incomplete"):
                fixture.build_queue(root / "queue")

    def test_model_package_report_and_weights_are_hash_bound(self) -> None:
        for filename in ("training_report.v1.json", "model.pt"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
                artifact = root / filename
                if artifact.suffix == ".json":
                    report = json.loads(artifact.read_text(encoding="utf-8"))
                    report["tampered"] = True
                    _write_json(artifact, report)
                else:
                    artifact.write_bytes(artifact.read_bytes() + b"tamper")
                with self.assertRaisesRegex(SelectiveReviewError, "sha256 mismatch"):
                    fixture.build_queue(root / "queue")

    def test_model_package_tamper_after_queue_rolls_back_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            fixture.build_queue(root / "queue")
            queue_path = root / "queue" / "selective_review_queue.v1.json"
            actions = _complete_queue_actions(queue_path)
            actions_path = root / "actions.json"
            _write_json(
                actions_path,
                {"schema_version": "1.0", "artifact_type": "selective_review_actions", "actions": actions},
            )
            fixture.weights_path.write_bytes(fixture.weights_path.read_bytes() + b"tamper")
            output_dir = root / "round"
            with self.assertRaisesRegex(SelectiveReviewError, "weights sha256 mismatch"):
                _materialize(fixture, queue_path, actions_path, output_dir)
            self.assertFalse(output_dir.exists())

    def test_hashing_does_not_use_path_read_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            with patch.object(Path, "read_bytes", side_effect=AssertionError("must stream")):
                queue = fixture.build_queue(root / "queue")
            self.assertEqual(3, queue["candidate_count"])

    def test_build_queue_cli_success_and_json_only_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": None})
            command = [
                "--dataset-manifest",
                str(fixture.dataset_path),
                "--predictions",
                str(fixture.predictions_path),
                "--policy",
                str(fixture.policy_path),
                "--decisions",
                str(fixture.decisions_path),
                "--model-manifest",
                str(fixture.model_path),
                "--contract",
                str(fixture.contract_path),
                "--annotation-resolution",
                str(fixture.annotation_resolution_path),
                "--resolved-contract",
                str(fixture.resolved_contract_path),
                "--policy-roles",
                str(fixture.policy_roles_path),
                "--output-dir",
                str(root / "queue"),
                "--fps-override",
                "b=29.97",
                "--window-seconds",
                "7.5",
                "--max-windows",
                "30",
            ]
            success = _run_cli_main(build_cli_main, command)
            self.assertEqual(0, success.returncode, success.stderr)
            self.assertEqual("", success.stderr)
            self.assertTrue(json.loads(success.stdout)["ok"])

            help_result = _run_cli_main(build_cli_main, ["--help"])
            self.assertEqual(0, help_result.returncode)
            self.assertEqual("", help_result.stderr)
            self.assertIn("usage:", help_result.stdout.lower())

            failure = _run_cli_main(build_cli_main, ["--dataset-manifest", str(fixture.dataset_path)])
            self.assertNotEqual(0, failure.returncode)
            self.assertEqual("", failure.stdout)
            self.assertEqual(1, len(failure.stderr.splitlines()))
            self.assertFalse(json.loads(failure.stderr)["ok"])
            self.assertNotIn("usage:", failure.stderr.lower())

    def test_materialize_cli_success_and_json_only_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            fixture.build_queue(root / "queue")
            queue_path = root / "queue" / "selective_review_queue.v1.json"
            actions = _complete_queue_actions(queue_path)
            actions_path = root / "actions.json"
            _write_json(
                actions_path,
                {"schema_version": "1.0", "artifact_type": "selective_review_actions", "actions": actions},
            )
            command = [
                "--queue",
                str(queue_path),
                "--actions",
                str(actions_path),
                "--dataset-manifest",
                str(fixture.dataset_path),
                "--predictions",
                str(fixture.predictions_path),
                "--policy",
                str(fixture.policy_path),
                "--decisions",
                str(fixture.decisions_path),
                "--model-manifest",
                str(fixture.model_path),
                "--contract",
                str(fixture.contract_path),
                "--annotation-resolution",
                str(fixture.annotation_resolution_path),
                "--resolved-contract",
                str(fixture.resolved_contract_path),
                "--policy-roles",
                str(fixture.policy_roles_path),
                "--output-dir",
                str(root / "round"),
            ]
            success = _run_cli_main(materialize_cli_main, command)
            self.assertEqual(0, success.returncode, success.stderr)
            self.assertEqual("", success.stderr)
            self.assertTrue(json.loads(success.stdout)["ok"])

            help_result = _run_cli_main(materialize_cli_main, ["--help"])
            self.assertEqual(0, help_result.returncode)
            self.assertEqual("", help_result.stderr)
            self.assertIn("usage:", help_result.stdout.lower())

            failure = _run_cli_main(materialize_cli_main, ["--queue", str(queue_path)])
            self.assertNotEqual(0, failure.returncode)
            self.assertEqual("", failure.stdout)
            self.assertEqual(1, len(failure.stderr.splitlines()))
            self.assertFalse(json.loads(failure.stderr)["ok"])
            self.assertNotIn("usage:", failure.stderr.lower())

    def test_timing_requires_override_when_dataset_fps_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": None})
            with self.assertRaisesRegex(SelectiveReviewError, "fps override"):
                fixture.build_queue(root / "queue")
            queue = fixture.build_queue(root / "queue-with-override", fps_overrides={"b": 29.97})
            timing = json.loads((root / "queue-with-override" / "review_timing.v1.json").read_text())
            self.assertEqual(
                "explicit_override", {row["variant_id"]: row for row in timing["variants"]}["b"]["fps_source"]
            )
            self.assertEqual(29.97, {row["variant_id"]: row for row in timing["variants"]}["b"]["fps"])
            self.assertLessEqual(queue["review_item_count"], 30)
            covered = {candidate["variant_id"] for item in queue["items"] for candidate in item["candidates"]}
            self.assertEqual({"a", "b"}, covered)

    def test_video_may_be_manifest_relative_outside_dataset_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            dataset_root = root / "dataset"
            fixture = _Fixture(dataset_root, fps_by_variant={"a": 20.0, "b": 20.0})
            video_root = root / "videos"
            video_root.mkdir()
            for source in fixture.dataset["sources"]:
                filename = source["path"]
                source_path = dataset_root / filename
                if not source_path.is_file():
                    continue
                source_path.replace(video_root / filename)
                source["path"] = f"../videos/{filename}"
            _write_json(fixture.dataset_path, fixture.dataset)
            dataset_sha256 = _sha256(fixture.dataset_path)
            resolution = json.loads(fixture.annotation_resolution_path.read_text(encoding="utf-8"))
            resolution["source_dataset_manifest"]["sha256"] = dataset_sha256
            _write_json(fixture.annotation_resolution_path, resolution)
            roles = json.loads(fixture.policy_roles_path.read_text(encoding="utf-8"))
            roles["lineage"]["dataset_manifest_sha256"] = dataset_sha256
            roles["lineage"]["annotation_resolution_sha256"] = _sha256(fixture.annotation_resolution_path)
            _write_json(fixture.policy_roles_path, roles)
            policy = json.loads(fixture.policy_path.read_text(encoding="utf-8"))
            decisions = json.loads(fixture.decisions_path.read_text(encoding="utf-8"))
            lineage = deepcopy(policy["lineage"])
            lineage["dataset_manifest"]["sha256"] = dataset_sha256
            lineage["annotation_resolution"]["sha256"] = _sha256(fixture.annotation_resolution_path)
            lineage["policy_roles"]["sha256"] = _sha256(fixture.policy_roles_path)
            policy["lineage"] = lineage
            policy["version_inputs"]["lineage"] = deepcopy(lineage)
            decisions["lineage"] = deepcopy(lineage)
            _write_json(fixture.decisions_path, decisions)
            _reseal_policy_decisions(fixture.policy_path, fixture.decisions_path, policy)

            fixture.build_queue(root / "queue")
            timing = json.loads((root / "queue" / "review_timing.v1.json").read_text(encoding="utf-8"))
            self.assertEqual(
                {"../videos/a.mp4", "../videos/b.mp4"},
                {variant["video"]["path"] for variant in timing["variants"]},
            )

    def test_materializes_valid_actions_without_mutating_source_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 29.97})
            queue_dir = root / "queue"
            fixture.build_queue(queue_dir)
            queue_path = queue_dir / "selective_review_queue.v1.json"
            queue = json.loads(queue_path.read_text())
            original_contract = fixture.contract_path.read_bytes()
            items_by_candidate = {
                candidate["candidate_id"]: (item, candidate)
                for item in queue["items"]
                for candidate in item["candidates"]
            }
            actions = []
            for action_id, candidate_id, action, extra in (
                ("act-confirm", "a-edge", "confirm_ball", {}),
                ("act-reject", "b-mid", "reject_noise", {"noise_subtype": "equipment_or_background"}),
                (
                    "act-correct",
                    "a-mid",
                    "correct_trajectory",
                    {"keypoints": [{"frame_index": 180, "status": "detected", "x": 100.0, "y": 120.0}]},
                ),
            ):
                item, candidate = items_by_candidate[candidate_id]
                actions.append(
                    {
                        "action_id": action_id,
                        "review_item_id": item["review_item_id"],
                        "candidate_id": candidate_id,
                        "reviewer_id": "reviewer-1",
                        "created_at": "2026-07-09T12:00:00Z",
                        "action": action,
                        "bindings": {
                            "queue_sha256": _sha256(queue_path),
                            "timing_sha256": queue["bindings"]["review_timing"]["sha256"],
                            "policy_sha256": queue["bindings"]["policy"]["sha256"],
                            "decisions_sha256": queue["bindings"]["decisions"]["sha256"],
                            "model_sha256": queue["bindings"]["model"]["sha256"],
                            "training_report_sha256": queue["bindings"]["training_report"]["sha256"],
                            "model_weights_sha256": queue["bindings"]["model_weights"]["sha256"],
                            "dataset_sha256": queue["bindings"]["dataset"]["sha256"],
                            "predictions_sha256": queue["bindings"]["predictions"]["sha256"],
                            "contract_sha256": queue["bindings"]["contract"]["sha256"],
                            "annotation_resolution_sha256": queue["bindings"]["annotation_resolution"]["sha256"],
                            "resolved_tracking_contract_sha256": queue["bindings"]["resolved_tracking_contract"][
                                "sha256"
                            ],
                            "policy_roles_sha256": queue["bindings"]["policy_roles"]["sha256"],
                            "evidence_sha256": candidate["evidence"]["sha256"],
                            "candidate_fingerprint": candidate["candidate_fingerprint"],
                        },
                        **extra,
                    }
                )
            actions_path = root / "actions.json"
            _write_json(
                actions_path,
                {"schema_version": "1.0", "artifact_type": "selective_review_actions", "actions": actions},
            )
            output_dir = root / "round"
            with patch("football_tracking.candidate_classifier.train_candidate_classifier") as trainer:
                report = materialize_selective_review_actions(
                    queue_path,
                    actions_path,
                    fixture.dataset_path,
                    fixture.predictions_path,
                    fixture.policy_path,
                    fixture.model_path,
                    fixture.contract_path,
                    output_dir,
                    decisions_path=fixture.decisions_path,
                    annotation_resolution_path=fixture.annotation_resolution_path,
                    resolved_contract_path=fixture.resolved_contract_path,
                    policy_roles_path=fixture.policy_roles_path,
                )
                trainer.assert_not_called()
            self.assertTrue({"model", "training_report", "model_weights"}.issubset(report["bindings"]))
            self.assertEqual(original_contract, fixture.contract_path.read_bytes())
            self.assertFalse(report["training_invoked"])
            votes = (output_dir / "human_adjudication_votes.v1.jsonl").read_text().splitlines()
            self.assertEqual(3, len(votes))  # header plus two label actions
            corrections = json.loads((output_dir / "trajectory_corrections.v1.json").read_text())
            self.assertEqual("abstain", corrections["corrections"][0]["selective_decision"])
            derived = json.loads((output_dir / "annotations" / "tracking_contract.v2.json").read_text())
            self.assertEqual([], derived["frames"])
            self.assertIn("annotation_adjudication_queue", report["artifacts"])
            for descriptor in report["artifacts"].values():
                artifact = output_dir / descriptor["path"]
                self.assertTrue(artifact.is_file())
                self.assertEqual(descriptor["sha256"], _sha256(artifact))

    def test_materialization_requires_exact_action_coverage_for_nonempty_queue(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            fixture.build_queue(root / "queue")
            queue_path = root / "queue" / "selective_review_queue.v1.json"
            for name, actions in (
                ("empty", []),
                ("partial", [_queue_action(queue_path, "a-edge", "mark_unknown")]),
            ):
                with self.subTest(name=name):
                    actions_path = root / f"actions-{name}.json"
                    _write_json(
                        actions_path,
                        {
                            "schema_version": "1.0",
                            "artifact_type": "selective_review_actions",
                            "actions": actions,
                        },
                    )
                    with self.assertRaisesRegex(SelectiveReviewError, "action coverage.*missing"):
                        _materialize(fixture, queue_path, actions_path, root / f"round-{name}")

    def test_materialization_accepts_exact_empty_actions_for_zero_candidate_queue(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(
                root,
                fps_by_variant={"a": 20.0, "b": 20.0},
                include_application_candidates=False,
            )
            queue = fixture.build_queue(root / "queue")
            queue_path = root / "queue" / "selective_review_queue.v1.json"
            self.assertEqual([], queue["items"])
            self.assertEqual(0, queue["review_item_count"])
            self.assertEqual(0, queue["candidate_count"])
            self.assertEqual(0, queue["selection"]["counts"]["eligible"])
            actions_path = root / "actions-empty-queue.json"
            _write_json(
                actions_path,
                {
                    "schema_version": "1.0",
                    "artifact_type": "selective_review_actions",
                    "actions": [],
                },
            )

            report = _materialize(
                fixture,
                queue_path,
                actions_path,
                root / "round-empty-queue",
            )

            self.assertEqual(0, report["summary"]["action_count"])
            self.assertEqual(0, report["summary"]["vote_count"])
            votes = (root / "round-empty-queue" / "human_adjudication_votes.v1.jsonl").read_text().splitlines()
            self.assertEqual(1, len(votes))

    def test_reject_noise_requires_concrete_subtype_and_keypoints_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            fixture.build_queue(root / "queue")
            queue_path = root / "queue" / "selective_review_queue.v1.json"
            queue = json.loads(queue_path.read_text())
            item = next(
                item for item in queue["items"] if any(c["candidate_id"] == "b-mid" for c in item["candidates"])
            )
            candidate = next(c for c in item["candidates"] if c["candidate_id"] == "b-mid")
            base = {
                "action_id": "bad",
                "review_item_id": item["review_item_id"],
                "candidate_id": "b-mid",
                "reviewer_id": "reviewer",
                "created_at": "2026-07-09T12:00:00Z",
                "action": "reject_noise",
                "bindings": {
                    "queue_sha256": _sha256(queue_path),
                    "timing_sha256": queue["bindings"]["review_timing"]["sha256"],
                    "policy_sha256": queue["bindings"]["policy"]["sha256"],
                    "decisions_sha256": queue["bindings"]["decisions"]["sha256"],
                    "model_sha256": queue["bindings"]["model"]["sha256"],
                    "training_report_sha256": queue["bindings"]["training_report"]["sha256"],
                    "model_weights_sha256": queue["bindings"]["model_weights"]["sha256"],
                    "dataset_sha256": queue["bindings"]["dataset"]["sha256"],
                    "predictions_sha256": queue["bindings"]["predictions"]["sha256"],
                    "contract_sha256": queue["bindings"]["contract"]["sha256"],
                    "annotation_resolution_sha256": queue["bindings"]["annotation_resolution"]["sha256"],
                    "resolved_tracking_contract_sha256": queue["bindings"]["resolved_tracking_contract"]["sha256"],
                    "policy_roles_sha256": queue["bindings"]["policy_roles"]["sha256"],
                    "evidence_sha256": candidate["evidence"]["sha256"],
                    "candidate_fingerprint": candidate["candidate_fingerprint"],
                },
            }
            actions_path = root / "actions.json"
            _write_json(
                actions_path, {"schema_version": "1.0", "artifact_type": "selective_review_actions", "actions": [base]}
            )
            with self.assertRaisesRegex(SelectiveReviewError, "noise_subtype"):
                materialize_selective_review_actions(
                    queue_path,
                    actions_path,
                    fixture.dataset_path,
                    fixture.predictions_path,
                    fixture.policy_path,
                    fixture.model_path,
                    fixture.contract_path,
                    root / "round",
                    decisions_path=fixture.decisions_path,
                    annotation_resolution_path=fixture.annotation_resolution_path,
                    resolved_contract_path=fixture.resolved_contract_path,
                    policy_roles_path=fixture.policy_roles_path,
                )

    def test_queue_uses_independent_decisions_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            queue = fixture.build_queue(root / "queue")
            self.assertEqual("independent_artifact", queue["bindings"]["decisions"]["source"])
            self.assertEqual(_sha256(fixture.decisions_path), queue["bindings"]["decisions"]["sha256"])
            self.assertNotIn("decisions", json.loads(fixture.policy_path.read_text(encoding="utf-8")))

            action = _queue_action(root / "queue" / "selective_review_queue.v1.json", "a-edge", "mark_unknown")
            actions_path = root / "actions.json"
            _write_json(
                actions_path,
                {"schema_version": "1.0", "artifact_type": "selective_review_actions", "actions": [action]},
            )
            with self.assertRaisesRegex(SelectiveReviewError, "independent selective decisions"):
                materialize_selective_review_actions(
                    root / "queue" / "selective_review_queue.v1.json",
                    actions_path,
                    fixture.dataset_path,
                    fixture.predictions_path,
                    fixture.policy_path,
                    fixture.model_path,
                    fixture.contract_path,
                    root / "round",
                )

    def test_keypoints_reject_out_of_bounds_and_nonfinite_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            fixture.build_queue(root / "queue")
            queue_path = root / "queue" / "selective_review_queue.v1.json"
            actions_path = root / "actions.json"
            out_of_bounds = _queue_action(
                queue_path,
                "a-mid",
                "correct_trajectory",
                keypoints=[{"frame_index": 180, "status": "detected", "x": 640.0, "y": 120.0}],
            )
            _write_json(
                actions_path,
                {
                    "schema_version": "1.0",
                    "artifact_type": "selective_review_actions",
                    "actions": [out_of_bounds],
                },
            )
            with self.assertRaisesRegex(SelectiveReviewError, "source dimensions"):
                _materialize(fixture, queue_path, actions_path, root / "round-oob")

            nonfinite = _queue_action(
                queue_path,
                "a-mid",
                "correct_trajectory",
                keypoints=[{"frame_index": 180, "status": "detected", "x": math.nan, "y": 120.0}],
            )
            actions_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "artifact_type": "selective_review_actions",
                        "actions": [nonfinite],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SelectiveReviewError, "non-finite"):
                _materialize(fixture, queue_path, actions_path, root / "round-nonfinite")

    def test_conflicting_candidate_actions_and_binding_tamper_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            fixture.build_queue(root / "queue")
            queue_path = root / "queue" / "selective_review_queue.v1.json"
            actions_path = root / "actions.json"
            actions = [
                _queue_action(queue_path, "a-edge", "confirm_ball", action_id="confirm"),
                _queue_action(queue_path, "a-edge", "mark_unknown", action_id="unknown"),
            ]
            _write_json(
                actions_path,
                {"schema_version": "1.0", "artifact_type": "selective_review_actions", "actions": actions},
            )
            with self.assertRaisesRegex(SelectiveReviewError, "conflicting actions"):
                _materialize(fixture, queue_path, actions_path, root / "round-conflict")

            tampered = _queue_action(queue_path, "a-edge", "mark_unknown")
            tampered["bindings"]["decisions_sha256"] = "0" * 64
            _write_json(
                actions_path,
                {"schema_version": "1.0", "artifact_type": "selective_review_actions", "actions": [tampered]},
            )
            with self.assertRaisesRegex(SelectiveReviewError, "decisions_sha256"):
                _materialize(fixture, queue_path, actions_path, root / "round-tamper")

    def test_queue_cannot_override_round_queue_or_action_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            fixture.build_queue(root / "queue")
            queue_path = root / "queue" / "selective_review_queue.v1.json"
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            queue["bindings"]["queue"] = {"path": "forged", "sha256": "0" * 64}
            _write_json(queue_path, queue)
            actions = _complete_queue_actions(queue_path)
            actions_path = root / "actions.json"
            _write_json(
                actions_path,
                {"schema_version": "1.0", "artifact_type": "selective_review_actions", "actions": actions},
            )
            with self.assertRaisesRegex(SelectiveReviewError, "binding keys"):
                _materialize(fixture, queue_path, actions_path, root / "round")

    def test_base_exception_rolls_back_entire_round(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            fixture.build_queue(root / "queue")
            queue_path = root / "queue" / "selective_review_queue.v1.json"
            actions = _complete_queue_actions(queue_path)
            actions_path = root / "actions.json"
            _write_json(
                actions_path,
                {"schema_version": "1.0", "artifact_type": "selective_review_actions", "actions": actions},
            )
            output_dir = root / "round"
            with (
                patch(
                    "football_tracking.selective_review.resolve_candidate_annotations",
                    side_effect=KeyboardInterrupt,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                _materialize(fixture, queue_path, actions_path, output_dir)
            self.assertFalse(output_dir.exists())
            self.assertEqual([], list(root.glob(".round.staging-*")))

    def test_input_mutation_rolls_back_entire_round(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fixture = _Fixture(root, fps_by_variant={"a": 20.0, "b": 20.0})
            fixture.build_queue(root / "queue")
            queue_path = root / "queue" / "selective_review_queue.v1.json"
            actions = _complete_queue_actions(queue_path)
            actions_path = root / "actions.json"
            _write_json(
                actions_path,
                {"schema_version": "1.0", "artifact_type": "selective_review_actions", "actions": actions},
            )

            def resolve_then_mutate(*args: object, **kwargs: object) -> dict[str, object]:
                report = resolve_candidate_annotations(*args, **kwargs)
                fixture.policy_path.write_bytes(fixture.policy_path.read_bytes() + b" ")
                return report

            output_dir = root / "round"
            with (
                patch(
                    "football_tracking.selective_review.resolve_candidate_annotations",
                    side_effect=resolve_then_mutate,
                ),
                self.assertRaisesRegex(SelectiveReviewError, "input changed"),
            ):
                _materialize(fixture, queue_path, actions_path, output_dir)
            self.assertFalse(output_dir.exists())
            self.assertEqual([], list(root.glob(".round.staging-*")))

    def test_action_ids_are_idempotent_only_for_identical_payloads(self) -> None:
        from football_tracking.selective_review import deduplicate_actions

        action = {"action_id": "same", "action": "mark_unknown", "value": 1}
        self.assertEqual([action], deduplicate_actions([action, dict(action)]))
        with self.assertRaisesRegex(SelectiveReviewError, "conflicting payload"):
            deduplicate_actions([action, {**action, "value": 2}])
