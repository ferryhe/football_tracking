from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import platform
import random
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F

from football_tracking.tracking_contracts import (
    CLASSIFICATION_LABELS,
    CONFIRMED_LABEL_ORIGINS,
    TRACKING_CONTRACT_REPORT_NAME,
    build_tracking_contract,
    load_tracking_contract,
)

MODEL_WEIGHTS_NAME = "model.pt"
MODEL_MANIFEST_NAME = "model_manifest.v1.json"
TRAINING_REPORT_NAME = "training_report.v1.json"
PREDICTIONS_NAME = "candidate_predictions.v1.json"
MODEL_SCHEMA_VERSION = "1.0"
PREDICTIONS_SCHEMA_VERSION = "1.0"
CLASS_LABELS = tuple(CLASSIFICATION_LABELS)
TIGHT_SHAPE = (5, 3, 64, 64)
CONTEXT_SHAPE = (5, 3, 128, 128)
METADATA_DIM = 7
_NOISE_LABELS = frozenset(CLASS_LABELS) - {"match_ball", "unknown"}
_MAX_NPY_OVERHEAD_BYTES = 128 * 1024


class ClassifierError(RuntimeError):
    """Raised when classifier inputs or packages fail closed validation."""


@dataclass(frozen=True)
class TrainingConfig:
    epochs: int = 3
    batch_size: int = 8
    learning_rate: float = 0.005
    weight_decay: float = 0.0
    seed: int = 1337


class _ImageBranch(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.layers = nn.Sequential(
            nn.Conv2d(15, 8, kernel_size=3, stride=2, padding=1),
            nn.ReLU(),
            nn.Conv2d(8, 12, kernel_size=3, stride=2, padding=1),
            nn.ReLU(),
            nn.AdaptiveAvgPool2d(1),
        )

    def forward(self, value: torch.Tensor) -> torch.Tensor:
        return self.layers(value).flatten(1)


class CandidateClassifier(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.tight_branch = _ImageBranch()
        self.context_branch = _ImageBranch()
        self.head = nn.Sequential(
            nn.Linear(24 + METADATA_DIM, 32),
            nn.ReLU(),
            nn.Linear(32, len(CLASS_LABELS)),
        )

    def forward(
        self,
        tight: torch.Tensor,
        context: torch.Tensor,
        metadata: torch.Tensor,
    ) -> torch.Tensor:
        _validate_model_inputs(tight, context, metadata)
        tight_features = self.tight_branch(tight.reshape(tight.shape[0], 15, 64, 64))
        context_features = self.context_branch(context.reshape(context.shape[0], 15, 128, 128))
        return self.head(torch.cat((tight_features, context_features, metadata), dim=1))


def train_candidate_classifier(
    dataset_manifest_path: Path,
    annotation_resolution_path: Path,
    resolved_contract_path: Path,
    output_dir: Path,
    *,
    config: TrainingConfig | None = None,
) -> dict[str, Any]:
    config = _validated_config(config or TrainingConfig())
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise ClassifierError(f"output directory already exists: {output_dir}")

    dataset_path, dataset, dataset_sha256 = _load_dataset_manifest(dataset_manifest_path)
    resolution_path, resolution, resolution_sha256 = _load_resolution(annotation_resolution_path)
    contract_path, contract, contract_sha256 = _load_contract(resolved_contract_path)
    input_bindings = {
        "candidate dataset manifest": (dataset_path, dataset_sha256),
        "annotation resolution": (resolution_path, resolution_sha256),
        "resolved tracking contract": (contract_path, contract_sha256),
    }
    _verify_resolution_bindings(dataset, resolution, dataset_sha256, contract_sha256)
    _validate_candidate_identities(dataset["samples"], contract["candidates"], dataset["sources"])
    selected, truth_report = _select_training_truth(dataset, resolution, contract)
    split = _build_split(selected, dataset, seed=config.seed)
    examples = _load_examples(dataset_path.parent, dataset["samples"], selected)
    examples_by_id = {item["candidate_id"]: item for item in examples}
    train_examples = [
        examples_by_id[candidate_id] for candidate_id, name in split["assignments"].items() if name == "train"
    ]
    calibration_examples = [
        examples_by_id[candidate_id] for candidate_id, name in split["assignments"].items() if name == "calibration"
    ]
    test_examples = [
        examples_by_id[candidate_id] for candidate_id, name in split["assignments"].items() if name == "test"
    ]
    _require_match_and_noise(train_examples, "training split")

    _set_deterministic_seed(config.seed)
    model = CandidateClassifier().to("cpu")
    parameter_count = sum(parameter.numel() for parameter in model.parameters())
    if parameter_count >= 100_000:
        raise ClassifierError(f"classifier parameter budget exceeded: {parameter_count}")
    supported_mask, class_weights = _training_support(train_examples)
    loss_history = _fit_model(model, train_examples, supported_mask, class_weights, config)

    calibration_logits, calibration_targets = _predict_logits(model, calibration_examples, config.batch_size)
    temperature, before_metrics, after_metrics = _calibrate(
        calibration_logits,
        calibration_targets,
        supported_mask,
    )
    test_logits, test_targets = _predict_logits(model, test_examples, config.batch_size)
    test_metrics = _classification_metrics(test_logits, test_targets, supported_mask, temperature)

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    try:
        weights_path = staging_dir / MODEL_WEIGHTS_NAME
        state_dict = {name: value.detach().cpu() for name, value in model.state_dict().items()}
        torch.save(state_dict, weights_path)
        weights_sha256 = _sha256_file(weights_path)
        runtime = _runtime_fingerprint()
        architecture = _architecture_contract(parameter_count)
        input_contract = _input_contract(dataset)
        calibration = {
            "temperature": temperature,
            "before": before_metrics,
            "after": after_metrics,
        }
        data_binding = {
            "dataset_version": dataset["dataset_version"],
            "dataset_manifest_sha256": dataset_sha256,
            "annotation_resolution_sha256": resolution_sha256,
            "resolved_contract_sha256": contract_sha256,
        }
        version_inputs = {
            "weights_sha256": weights_sha256,
            "data_binding": data_binding,
            "training_config": asdict(config),
            "calibration": calibration,
            "supported_mask": supported_mask,
            "class_order": list(CLASS_LABELS),
            "architecture": architecture,
            "input_contract": input_contract,
            "code_sha256": _sha256_file(Path(__file__)),
            "runtime": runtime,
        }
        model_version = _canonical_sha256(version_inputs)
        report = {
            "schema_version": MODEL_SCHEMA_VERSION,
            "artifact_type": "candidate_classifier_training_report",
            "status": "complete",
            "model_version": model_version,
            "training_config": asdict(config),
            "truth_selection": truth_report,
            "split": split,
            "class_order": list(CLASS_LABELS),
            "supported_classes": [label for label, supported in zip(CLASS_LABELS, supported_mask) if supported],
            "supported_mask": supported_mask,
            "class_weights": class_weights,
            "loss_history": loss_history,
            "calibration": calibration,
            "test_metrics": test_metrics,
            "data_binding": data_binding,
        }
        report_sha256 = hashlib.sha256(_json_bytes(report)).hexdigest()
        manifest = {
            "schema_version": MODEL_SCHEMA_VERSION,
            "artifact_type": "candidate_classifier_model",
            "model_version": model_version,
            "weights_path": MODEL_WEIGHTS_NAME,
            "weights_sha256": weights_sha256,
            "training_report_path": TRAINING_REPORT_NAME,
            "training_report_sha256": report_sha256,
            "class_order": list(CLASS_LABELS),
            "supported_classes": report["supported_classes"],
            "supported_mask": supported_mask,
            "architecture": architecture,
            "input_contract": input_contract,
            "preprocessing": input_contract["semantic_preprocessing"],
            "state_shapes": {name: list(value.shape) for name, value in state_dict.items()},
            "calibration": calibration,
            "data_binding": data_binding,
            "training_config": asdict(config),
            "seed": config.seed,
            "code_sha256": version_inputs["code_sha256"],
            "runtime": runtime,
        }
        _write_json_file(staging_dir / MODEL_MANIFEST_NAME, manifest)
        _write_json_file(staging_dir / TRAINING_REPORT_NAME, report)
        _verify_file_bindings(input_bindings)
        os.replace(staging_dir, output_dir)
        return report
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def load_candidate_classifier(package_dir: Path) -> tuple[CandidateClassifier, dict[str, Any]]:
    model, manifest, _ = _load_candidate_classifier_with_bindings(package_dir)
    return model, manifest


def _load_candidate_classifier_with_bindings(
    package_dir: Path,
) -> tuple[CandidateClassifier, dict[str, Any], dict[str, tuple[Path, str]]]:
    package_dir = Path(package_dir).resolve()
    manifest_path = package_dir / MODEL_MANIFEST_NAME
    weights_path = package_dir / MODEL_WEIGHTS_NAME
    report_path = package_dir / TRAINING_REPORT_NAME
    package_bindings = {
        "model manifest": (manifest_path, _sha256_file(manifest_path)),
        "model weights": (weights_path, _sha256_file(weights_path)),
        "training report": (report_path, _sha256_file(report_path)),
    }
    manifest = _load_json_object(manifest_path, "model manifest")
    if manifest.get("schema_version") != MODEL_SCHEMA_VERSION:
        raise ClassifierError("unsupported model manifest schema_version")
    if manifest.get("class_order") != list(CLASS_LABELS):
        raise ClassifierError("model class order does not match the ordered V2 label contract")
    if (
        manifest.get("weights_path") != MODEL_WEIGHTS_NAME
        or manifest.get("training_report_path") != TRAINING_REPORT_NAME
    ):
        raise ClassifierError("model package contains unsafe artifact paths")
    if package_bindings["model weights"][1] != manifest.get("weights_sha256"):
        raise ClassifierError("model weights sha256 mismatch")
    if package_bindings["training report"][1] != manifest.get("training_report_sha256"):
        raise ClassifierError("training report sha256 mismatch")
    _validate_input_contract(manifest.get("input_contract"))
    if manifest.get("preprocessing") != manifest["input_contract"]["semantic_preprocessing"]:
        raise ClassifierError("model preprocessing fields are inconsistent")
    supported_mask = _validated_supported_mask(manifest.get("supported_mask"))
    expected_supported_classes = [label for label, supported in zip(CLASS_LABELS, supported_mask) if supported]
    if manifest.get("supported_classes") != expected_supported_classes:
        raise ClassifierError("supported_classes does not match supported_mask")
    training_config = manifest.get("training_config")
    if not isinstance(training_config, dict) or manifest.get("seed") != training_config.get("seed"):
        raise ClassifierError("model seed and training_config are inconsistent")
    if manifest.get("runtime", {}).get("device") != "cpu":
        raise ClassifierError("model package is not CPU-bound")
    temperature = _positive_finite(manifest.get("calibration", {}).get("temperature"), "temperature")
    version_inputs = {
        "weights_sha256": manifest.get("weights_sha256"),
        "data_binding": manifest.get("data_binding"),
        "training_config": manifest.get("training_config"),
        "calibration": manifest.get("calibration"),
        "supported_mask": supported_mask,
        "class_order": manifest.get("class_order"),
        "architecture": manifest.get("architecture"),
        "input_contract": manifest.get("input_contract"),
        "code_sha256": manifest.get("code_sha256"),
        "runtime": manifest.get("runtime"),
    }
    if _canonical_sha256(version_inputs) != manifest.get("model_version"):
        raise ClassifierError("model_version does not match package content")

    model = CandidateClassifier().to("cpu")
    parameter_count = sum(parameter.numel() for parameter in model.parameters())
    if manifest.get("architecture") != _architecture_contract(parameter_count):
        raise ClassifierError("model architecture contract mismatch")
    expected_shapes = {name: list(value.shape) for name, value in model.state_dict().items()}
    if manifest.get("state_shapes") != expected_shapes:
        raise ClassifierError("model state shape contract mismatch")
    try:
        state = torch.load(weights_path, map_location="cpu", weights_only=True)
    except Exception as exc:
        raise ClassifierError(f"could not load model weights: {exc}") from exc
    if not isinstance(state, dict) or set(state) != set(expected_shapes):
        raise ClassifierError("model state keys mismatch")
    for name, value in state.items():
        if not isinstance(value, torch.Tensor) or list(value.shape) != expected_shapes[name]:
            raise ClassifierError(f"model state tensor shape mismatch: {name}")
        if not torch.isfinite(value).all():
            raise ClassifierError(f"model state contains non-finite values: {name}")
    model.load_state_dict(state, strict=True)
    model.eval()
    report = _load_json_object(report_path, "training report")
    if (
        report.get("schema_version") != MODEL_SCHEMA_VERSION
        or report.get("artifact_type") != "candidate_classifier_training_report"
        or report.get("status") != "complete"
    ):
        raise ClassifierError("training report envelope is invalid")
    for field in (
        "model_version",
        "class_order",
        "supported_classes",
        "supported_mask",
        "calibration",
        "data_binding",
        "training_config",
    ):
        manifest_value = manifest.get(field)
        if report.get(field) != manifest_value:
            raise ClassifierError(f"model manifest and training report disagree on {field}")
    manifest["calibration"]["temperature"] = temperature
    _verify_file_bindings(package_bindings)
    return model, manifest, package_bindings


def classify_candidates(
    package_dir: Path,
    dataset_manifest_path: Path,
    source_contract_path: Path,
    output_dir: Path,
    *,
    batch_size: int = 32,
) -> dict[str, Any]:
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
        raise ClassifierError("batch_size must be a positive integer")
    model, model_manifest, package_bindings = _load_candidate_classifier_with_bindings(package_dir)
    dataset_path, dataset, dataset_sha256 = _load_dataset_manifest(dataset_manifest_path)
    contract_path, contract, contract_sha256 = _load_contract(source_contract_path)
    input_bindings = {
        **package_bindings,
        "candidate dataset manifest": (dataset_path, dataset_sha256),
        "source tracking contract": (contract_path, contract_sha256),
    }
    _validate_inference_preprocessing(dataset, model_manifest)
    samples = dataset["samples"]
    sample_ids = {sample["candidate_id"] for sample in samples}
    contract_ids = {candidate["candidate_id"] for candidate in contract["candidates"]}
    if sample_ids != contract_ids:
        raise ClassifierError(
            f"dataset/contract candidate mismatch: missing={sorted(contract_ids - sample_ids)}, "
            f"dangling={sorted(sample_ids - contract_ids)}"
        )
    fingerprints = _validate_candidate_identities(samples, contract["candidates"], dataset["sources"])
    examples = _load_examples(dataset_path.parent, samples, None)
    logits, _ = _predict_logits(model, examples, batch_size)
    supported_mask = torch.tensor(model_manifest["supported_mask"], dtype=torch.bool)
    temperature = float(model_manifest["calibration"]["temperature"])
    probabilities = torch.softmax(_masked_logits(logits, supported_mask) / temperature, dim=1)
    if not torch.isfinite(probabilities).all():
        raise ClassifierError("inference produced non-finite probabilities")
    predictions = []
    for example, values in zip(examples, probabilities):
        probability_values = [float(value) for value in values.tolist()]
        predicted_index = int(torch.argmax(values).item())
        predictions.append(
            {
                "candidate_id": example["candidate_id"],
                "candidate_fingerprint": fingerprints[example["candidate_id"]],
                "predicted_label": CLASS_LABELS[predicted_index],
                "confidence": probability_values[predicted_index],
                "probabilities": dict(zip(CLASS_LABELS, probability_values)),
                "model_version": model_manifest["model_version"],
            }
        )
    prediction_report = {
        "schema_version": PREDICTIONS_SCHEMA_VERSION,
        "artifact_type": "candidate_predictions",
        "model_version": model_manifest["model_version"],
        "dataset_version": dataset["dataset_version"],
        "source_contract_sha256": contract_sha256,
        "class_order": list(CLASS_LABELS),
        "temperature": temperature,
        "prediction_count": len(predictions),
        "predictions": predictions,
    }
    confirmed_ids = {
        row["candidate_id"] for row in contract["classifications"] if row.get("label_origin") in CONFIRMED_LABEL_ORIGINS
    }
    additions = [
        {
            "candidate_id": prediction["candidate_id"],
            "label": prediction["predicted_label"],
            "label_origin": "prelabel",
            "confidence": prediction["confidence"],
        }
        for prediction in predictions
        if prediction["candidate_id"] not in confirmed_ids
    ]
    derived_contract = build_tracking_contract(
        source=contract.get("source"),
        frames=contract["frames"],
        candidates=contract["candidates"],
        classifications=[*contract["classifications"], *additions],
        decisions=contract["decisions"],
    )
    if derived_contract["validation_errors"]:
        raise ClassifierError(f"derived tracking contract is invalid: {derived_contract['validation_errors']}")

    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise ClassifierError(f"output directory already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    try:
        _write_json_file(staging_dir / PREDICTIONS_NAME, prediction_report)
        _write_json_file(staging_dir / TRACKING_CONTRACT_REPORT_NAME, derived_contract)
        _verify_file_bindings(input_bindings)
        os.replace(staging_dir, output_dir)
        return prediction_report
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def _select_training_truth(
    dataset: dict[str, Any],
    resolution: dict[str, Any],
    contract: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    samples = dataset["samples"]
    sample_by_id = _unique_by(samples, "candidate_id", "dataset samples")
    contract_candidates = _unique_by(contract["candidates"], "candidate_id", "contract candidates")
    if set(sample_by_id) != set(contract_candidates):
        raise ClassifierError("dataset and resolved contract candidate ids must match exactly")
    resolution_by_id = _unique_by(resolution.get("resolutions"), "candidate_id", "annotation resolutions")
    if set(resolution_by_id) != set(sample_by_id):
        raise ClassifierError("annotation resolution candidate ids must match the dataset exactly")
    confirmed: dict[str, list[dict[str, Any]]] = {}
    for row in contract["classifications"]:
        if row.get("label_origin") in CONFIRMED_LABEL_ORIGINS:
            confirmed.setdefault(row["candidate_id"], []).append(row)

    selected: list[dict[str, Any]] = []
    excluded: dict[str, list[str]] = {}
    for candidate_id, sample in sample_by_id.items():
        reasons: list[str] = []
        item = resolution_by_id.get(candidate_id)
        if item is None:
            reasons.append("missing_resolution")
        else:
            if item.get("status") != "confirmed" or item.get("training_eligible") is not True:
                reasons.append("resolution_not_training_eligible")
                reasons.extend(str(reason) for reason in item.get("reasons", []) if isinstance(reason, str))
            label = item.get("label")
            origin = item.get("label_origin")
            if label not in CLASS_LABELS:
                reasons.append("invalid_label")
            if origin not in CONFIRMED_LABEL_ORIGINS:
                reasons.append("label_not_confirmed")
            if label == "unknown" and origin != "human_confirmed":
                reasons.append("unknown_requires_human_confirmation")
            rows = confirmed.get(candidate_id, [])
            confirmed_labels = {row["label"] for row in rows}
            if not rows:
                reasons.append("missing_confirmed_contract_row")
            elif len(confirmed_labels) != 1:
                reasons.append("conflicting_confirmed_contract_rows")
            elif label not in confirmed_labels:
                reasons.append("resolution_contract_label_mismatch")
        if reasons:
            excluded[candidate_id] = _dedupe(reasons)
            continue
        selected.append(
            {
                **sample,
                "label": item["label"],
                "label_origin": item["label_origin"],
                "label_index": CLASS_LABELS.index(item["label"]),
            }
        )
    _require_match_and_noise(selected, "eligible training truth")
    return selected, {
        "eligible_count": len(selected),
        "excluded_count": len(excluded),
        "eligible_candidate_ids": sorted(item["candidate_id"] for item in selected),
        "excluded": excluded,
    }


def _build_split(
    selected: list[dict[str, Any]],
    dataset: dict[str, Any],
    *,
    seed: int,
) -> dict[str, Any]:
    sources = _unique_by(dataset["sources"], "variant_id", "dataset sources")
    ids = [item["candidate_id"] for item in selected]
    parent = {candidate_id: candidate_id for candidate_id in ids}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    evidence: dict[str, dict[str, Any]] = {}
    for item in selected:
        source = sources.get(item.get("variant_id"))
        if source is None:
            raise ClassifierError(f"sample {item['candidate_id']!r} references absent source variant")
        for key in ("group_id", "split_group", "temporal_group"):
            source_value = _required_text(source.get(key), f"source {key}")
            sample_value = _required_text(item.get(key), f"sample {key}")
            if sample_value != source_value:
                raise ClassifierError(f"sample {item['candidate_id']!r} {key} mismatches source manifest")
        evidence[item["candidate_id"]] = {
            "variant_id": source["variant_id"],
            "video_sha256": _required_sha256(source.get("sha256"), "source sha256"),
            "group_id": _required_text(source.get("group_id"), "source group_id"),
            "split_group": _required_text(source.get("split_group"), "source split_group"),
            "temporal_group": _required_text(source.get("temporal_group"), "source temporal_group"),
            "frame_index": int(item["frame_index"]),
            "frame_window": [int(item["frame_index"]) - 2, int(item["frame_index"]) + 2],
            "temporal_block": f"{source['sha256']}:{int(item['frame_index']) // 5}",
        }
    shared_evidence_keys = ("variant_id", "video_sha256", "group_id", "split_group", "temporal_group")
    first_seen: dict[tuple[str, str], str] = {}
    intervals_by_video: dict[str, list[tuple[int, int, str]]] = {}
    for candidate_id in ids:
        item_evidence = evidence[candidate_id]
        for key in shared_evidence_keys:
            evidence_key = (key, item_evidence[key])
            previous_id = first_seen.setdefault(evidence_key, candidate_id)
            if previous_id != candidate_id:
                union(previous_id, candidate_id)
            _observe_split_work(1)
        window_start, window_end = item_evidence["frame_window"]
        intervals_by_video.setdefault(item_evidence["video_sha256"], []).append(
            (window_start, window_end, candidate_id)
        )

    for intervals in intervals_by_video.values():
        intervals.sort()
        cluster_anchor: str | None = None
        cluster_end = -1
        for window_start, window_end, candidate_id in intervals:
            if cluster_anchor is not None and window_start <= cluster_end:
                union(cluster_anchor, candidate_id)
                cluster_end = max(cluster_end, window_end)
            else:
                cluster_anchor = candidate_id
                cluster_end = window_end
            _observe_split_work(1)
    components: dict[str, list[str]] = {}
    for candidate_id in ids:
        components.setdefault(find(candidate_id), []).append(candidate_id)
    component_values = [sorted(values) for values in components.values()]
    if len(component_values) < 3:
        raise ClassifierError("leakage-safe train/calibration/test split requires at least three source groups")
    component_values.sort(key=lambda values: hashlib.sha256(f"{seed}:{','.join(values)}".encode()).hexdigest())
    labels_by_id = {item["candidate_id"]: item["label"] for item in selected}
    component_labels = [
        frozenset(labels_by_id[candidate_id] for candidate_id in component) for component in component_values
    ]
    label_components = {
        label: [index for index, labels in enumerate(component_labels) if label in labels] for label in CLASS_LABELS
    }
    eligible_holdouts = [
        index
        for index, labels in enumerate(component_labels)
        if "match_ball" in labels
        and labels.intersection(_NOISE_LABELS)
        and all(len(label_components[label]) >= 2 for label in labels)
    ]
    chosen: tuple[int, int] | None = None
    for calibration_index in eligible_holdouts:
        forbidden_test_indices = {calibration_index}
        for label in component_labels[calibration_index]:
            if len(label_components[label]) == 2:
                forbidden_test_indices.update(label_components[label])
            _observe_split_work(1)
        for test_index in eligible_holdouts:
            _observe_split_work(1)
            if test_index in forbidden_test_indices:
                continue
            chosen = calibration_index, test_index
            break
        if chosen is not None:
            break
    if chosen is None:
        raise ClassifierError(
            "could not create leakage-safe calibration and test splits that each contain match_ball and noise"
        )
    assignments: dict[str, str] = {}
    groups = []
    for index, component in enumerate(component_values):
        split_name = "calibration" if index == chosen[0] else "test" if index == chosen[1] else "train"
        groups.append({"component_id": index, "split": split_name, "candidate_ids": component})
        assignments.update({candidate_id: split_name for candidate_id in component})
    support = {
        split_name: {
            label: sum(
                labels_by_id[candidate_id] == label
                for candidate_id, assigned_split in assignments.items()
                if assigned_split == split_name
            )
            for label in CLASS_LABELS
        }
        for split_name in ("train", "calibration", "test")
    }
    masked_classes = {
        split_name: [label for label, count in support["train"].items() if count == 0]
        for split_name in ("train", "calibration", "test")
    }
    training_labels = {label for label, count in support["train"].items() if count}
    for split_name in ("calibration", "test"):
        unsupported = {label for label, count in support[split_name].items() if count and label not in training_labels}
        if unsupported:
            raise ClassifierError(
                f"{split_name} contains classes unsupported by the training split: {sorted(unsupported)}"
            )
    evidence_by_split = {
        split_name: {
            "candidate_ids": sorted(candidate_id for candidate_id, value in assignments.items() if value == split_name),
            "variant_ids": sorted(
                {
                    evidence[candidate_id]["variant_id"]
                    for candidate_id, value in assignments.items()
                    if value == split_name
                }
            ),
            "video_sha256": sorted(
                {
                    evidence[candidate_id]["video_sha256"]
                    for candidate_id, value in assignments.items()
                    if value == split_name
                }
            ),
            "group_ids": sorted(
                {
                    evidence[candidate_id]["group_id"]
                    for candidate_id, value in assignments.items()
                    if value == split_name
                }
            ),
            "split_groups": sorted(
                {
                    evidence[candidate_id]["split_group"]
                    for candidate_id, value in assignments.items()
                    if value == split_name
                }
            ),
            "temporal_groups": sorted(
                {
                    evidence[candidate_id]["temporal_group"]
                    for candidate_id, value in assignments.items()
                    if value == split_name
                }
            ),
            "temporal_blocks": sorted(
                {
                    evidence[candidate_id]["temporal_block"]
                    for candidate_id, value in assignments.items()
                    if value == split_name
                }
            ),
        }
        for split_name in ("train", "calibration", "test")
    }
    violations = _split_violations(assignments, evidence)
    if violations:
        raise ClassifierError(f"split leakage detected: {violations}")
    return {
        "strategy": "connected_source_group_and_temporal_evidence_v1",
        "seed": seed,
        "assignments": dict(sorted(assignments.items())),
        "groups": groups,
        "sample_evidence": evidence,
        "evidence_by_split": evidence_by_split,
        "support": support,
        "masked_classes": masked_classes,
        "leakage_checks": {"passed": True, "violations": []},
    }


def _split_violations(assignments: dict[str, str], evidence: dict[str, dict[str, Any]]) -> list[str]:
    violations: list[str] = []
    ids = sorted(assignments)
    for key in ("variant_id", "video_sha256", "group_id", "split_group", "temporal_group"):
        owner_by_value: dict[str, tuple[str, str]] = {}
        for candidate_id in ids:
            value = evidence[candidate_id][key]
            split_name = assignments[candidate_id]
            owner = owner_by_value.setdefault(value, (split_name, candidate_id))
            if owner[0] != split_name:
                violations.append(f"{key}:{owner[1]}:{candidate_id}")
            _observe_split_work(1)

    intervals_by_video: dict[str, list[tuple[int, int, str]]] = {}
    for candidate_id in ids:
        window_start, window_end = evidence[candidate_id]["frame_window"]
        intervals_by_video.setdefault(evidence[candidate_id]["video_sha256"], []).append(
            (window_start, window_end, candidate_id)
        )
    for intervals in intervals_by_video.values():
        intervals.sort()
        furthest_by_split: dict[str, tuple[int, str]] = {}
        for window_start, window_end, candidate_id in intervals:
            split_name = assignments[candidate_id]
            for other_split, (other_end, other_id) in furthest_by_split.items():
                if other_split != split_name and other_end >= window_start:
                    violations.append(f"temporal_overlap:{other_id}:{candidate_id}")
            previous = furthest_by_split.get(split_name)
            if previous is None or window_end > previous[0]:
                furthest_by_split[split_name] = (window_end, candidate_id)
            _observe_split_work(1)
    return violations


def _observe_split_work(units: int) -> None:
    del units


def _load_examples(
    dataset_root: Path,
    samples: list[dict[str, Any]],
    selected: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    selected_by_id = None if selected is None else {item["candidate_id"]: item for item in selected}
    examples = []
    for sample in samples:
        candidate_id = sample["candidate_id"]
        if selected_by_id is not None and candidate_id not in selected_by_id:
            continue
        tight = _tensor_descriptor(dataset_root, sample, "tight_tensor", TIGHT_SHAPE)
        context = _tensor_descriptor(dataset_root, sample, "context_tensor", CONTEXT_SHAPE)
        bbox = sample.get("bbox_normalized")
        confidence = sample.get("confidence")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ClassifierError(f"sample {candidate_id!r} bbox_normalized must contain four values")
        values = [_finite_number(value, "bbox_normalized") for value in bbox]
        if any(not 0.0 <= value <= 1.0 for value in values):
            raise ClassifierError(f"sample {candidate_id!r} bbox_normalized must be within [0, 1]")
        confidence_value = _finite_number(confidence, "confidence")
        if not 0.0 <= confidence_value <= 1.0:
            raise ClassifierError(f"sample {candidate_id!r} confidence must be within [0, 1]")
        metadata = [*values, (values[0] + values[2]) / 2.0, (values[1] + values[3]) / 2.0, confidence_value]
        truth = None if selected_by_id is None else selected_by_id[candidate_id]
        examples.append(
            {
                "candidate_id": candidate_id,
                "tight_descriptor": tight,
                "context_descriptor": context,
                "metadata": metadata,
                "label": None if truth is None else truth["label"],
                "label_index": None if truth is None else truth["label_index"],
            }
        )
    return examples


def _tensor_descriptor(
    dataset_root: Path,
    sample: dict[str, Any],
    name: str,
    shape: tuple[int, ...],
) -> dict[str, Any]:
    artifact = sample.get("artifacts", {}).get(name)
    if not isinstance(artifact, dict):
        raise ClassifierError(f"sample {sample.get('candidate_id')!r} missing {name}")
    relative = Path(str(artifact.get("path") or ""))
    if relative.is_absolute() or ".." in relative.parts:
        raise ClassifierError(f"sample tensor path is unsafe: {relative}")
    path = (dataset_root / relative).resolve()
    if not path.is_relative_to(dataset_root.resolve()) or not path.is_file():
        raise ClassifierError(f"sample tensor is missing: {path}")
    if artifact.get("shape") != list(shape) or artifact.get("dtype") != "uint8":
        raise ClassifierError(f"sample tensor descriptor mismatch: {path}")
    try:
        size_bytes = path.stat().st_size
    except OSError as exc:
        raise ClassifierError(f"could not inspect sample tensor {path}: {exc}") from exc
    maximum_size = math.prod(shape) + _MAX_NPY_OVERHEAD_BYTES
    if size_bytes <= 0 or size_bytes > maximum_size:
        raise ClassifierError(f"sample tensor file size is outside its bounded contract: {path}")
    if _sha256_file(path) != artifact.get("sha256"):
        raise ClassifierError(f"sample tensor sha256 mismatch: {path}")
    return {
        "path": path,
        "sha256": artifact["sha256"],
        "shape": shape,
        "size_bytes": size_bytes,
    }


def _load_tensor_file(descriptor: dict[str, Any]) -> torch.Tensor:
    path = descriptor["path"]
    expected_size = descriptor["size_bytes"]
    try:
        with path.open("rb") as handle:
            payload = handle.read(expected_size + 1)
    except OSError as exc:
        raise ClassifierError(f"could not read sample tensor {path}: {exc}") from exc
    if len(payload) != expected_size or hashlib.sha256(payload).hexdigest() != descriptor["sha256"]:
        raise ClassifierError(f"sample tensor sha256 mismatch: {path}")
    try:
        value = np.load(io.BytesIO(payload), allow_pickle=False)
    except Exception as exc:
        raise ClassifierError(f"could not load sample tensor {path}: {exc}") from exc
    if not isinstance(value, np.ndarray) or value.shape != descriptor["shape"] or value.dtype != np.uint8:
        raise ClassifierError(f"sample tensor contract mismatch: {path}")
    return torch.from_numpy(value).to(dtype=torch.float32).div_(255.0)


def _fit_model(
    model: CandidateClassifier,
    examples: list[dict[str, Any]],
    supported_mask: list[bool],
    class_weights: list[float],
    config: TrainingConfig,
) -> list[float]:
    optimizer = torch.optim.AdamW(model.parameters(), lr=config.learning_rate, weight_decay=config.weight_decay)
    weights = torch.tensor(class_weights, dtype=torch.float32)
    mask = torch.tensor(supported_mask, dtype=torch.bool)
    generator = torch.Generator(device="cpu").manual_seed(config.seed)
    history = []
    for _ in range(config.epochs):
        model.train()
        order = torch.randperm(len(examples), generator=generator).tolist()
        losses = []
        for start in range(0, len(order), config.batch_size):
            batch = [examples[index] for index in order[start : start + config.batch_size]]
            tight, context, metadata, targets = _stack_batch(batch)
            optimizer.zero_grad(set_to_none=True)
            logits = _masked_logits(model(tight, context, metadata), mask)
            loss = F.cross_entropy(logits, targets, weight=weights)
            if not torch.isfinite(loss):
                raise ClassifierError("training loss became non-finite")
            loss.backward()
            optimizer.step()
            losses.append(float(loss.detach()))
        history.append(sum(losses) / len(losses))
    return history


def _predict_logits(
    model: CandidateClassifier,
    examples: list[dict[str, Any]],
    batch_size: int,
) -> tuple[torch.Tensor, torch.Tensor]:
    logits = []
    targets = []
    model.eval()
    with torch.no_grad():
        for start in range(0, len(examples), batch_size):
            batch = examples[start : start + batch_size]
            tight, context, metadata, target = _stack_batch(batch)
            logits.append(model(tight, context, metadata).cpu())
            if all(item["label_index"] is not None for item in batch):
                targets.append(target.cpu())
    if not logits:
        raise ClassifierError("classifier requires at least one example")
    target_tensor = torch.cat(targets) if targets else torch.empty(0, dtype=torch.long)
    return torch.cat(logits), target_tensor


def _stack_batch(examples: list[dict[str, Any]]) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    tight = torch.stack([_load_tensor_file(item["tight_descriptor"]) for item in examples]).to("cpu")
    context = torch.stack([_load_tensor_file(item["context_descriptor"]) for item in examples]).to("cpu")
    metadata = torch.tensor([item["metadata"] for item in examples], dtype=torch.float32, device="cpu")
    _observe_loaded_batch(len(examples))
    targets = torch.tensor([int(item["label_index"] or 0) for item in examples], dtype=torch.long)
    return tight, context, metadata, targets


def _observe_loaded_batch(loaded_sample_count: int) -> None:
    del loaded_sample_count


def _training_support(examples: list[dict[str, Any]]) -> tuple[list[bool], list[float]]:
    counts = [sum(item["label_index"] == index for item in examples) for index in range(len(CLASS_LABELS))]
    supported = [count > 0 for count in counts]
    weights = [len(examples) / (count * sum(supported)) if count else 0.0 for count in counts]
    return supported, weights


def _calibrate(
    logits: torch.Tensor,
    targets: torch.Tensor,
    supported_mask: list[bool],
) -> tuple[float, dict[str, Any], dict[str, Any]]:
    mask = torch.tensor(supported_mask, dtype=torch.bool)
    if targets.numel() == 0 or any(not supported_mask[int(target)] for target in targets):
        raise ClassifierError("calibration split must contain only classes supported by training")
    candidates = np.exp(np.linspace(math.log(0.25), math.log(4.0), 81))
    best_temperature = 1.0
    best_nll = _nll(logits, targets, mask, best_temperature)
    for value in candidates:
        nll = _nll(logits, targets, mask, float(value))
        if nll < best_nll:
            best_temperature, best_nll = float(value), nll
    before = _classification_metrics(logits, targets, supported_mask, 1.0)
    after = _classification_metrics(logits, targets, supported_mask, best_temperature)
    return best_temperature, before, after


def _classification_metrics(
    logits: torch.Tensor,
    targets: torch.Tensor,
    supported_mask: list[bool],
    temperature: float,
) -> dict[str, Any]:
    mask = torch.tensor(supported_mask, dtype=torch.bool)
    probabilities = torch.softmax(_masked_logits(logits, mask) / temperature, dim=1)
    predictions = torch.argmax(probabilities, dim=1)
    one_hot = F.one_hot(targets, num_classes=len(CLASS_LABELS)).float()
    confidence = torch.max(probabilities, dim=1).values
    correct = predictions.eq(targets)
    ece = 0.0
    for lower in torch.linspace(0.0, 0.9, 10):
        upper = lower + 0.1
        selected = (confidence > lower) & (confidence <= upper)
        if selected.any():
            ece += float(
                selected.float().mean() * torch.abs(correct[selected].float().mean() - confidence[selected].mean())
            )
    confusion = [[0 for _ in CLASS_LABELS] for _ in CLASS_LABELS]
    for truth, prediction in zip(targets.tolist(), predictions.tolist()):
        confusion[truth][prediction] += 1
    return {
        "nll": _nll(logits, targets, mask, temperature),
        "ece": ece,
        "brier": float(torch.mean(torch.sum((probabilities - one_hot) ** 2, dim=1))),
        "support": {label: int((targets == index).sum()) for index, label in enumerate(CLASS_LABELS)},
        "confusion": confusion,
        "example_count": int(targets.numel()),
    }


def _nll(logits: torch.Tensor, targets: torch.Tensor, mask: torch.Tensor, temperature: float) -> float:
    return float(F.cross_entropy(_masked_logits(logits, mask) / temperature, targets))


def _masked_logits(logits: torch.Tensor, supported_mask: torch.Tensor) -> torch.Tensor:
    return logits.masked_fill(~supported_mask.to(logits.device).unsqueeze(0), -torch.inf)


def _load_dataset_manifest(path: Path) -> tuple[Path, dict[str, Any], str]:
    path = Path(path).resolve()
    sha256 = _sha256_file(path)
    value = _load_json_object(path, "candidate dataset manifest")
    _verify_file_bindings({"candidate dataset manifest": (path, sha256)})
    if value.get("schema_version") != "1.0" or value.get("artifact_type") != "candidate_dataset":
        raise ClassifierError("invalid candidate dataset manifest envelope")
    if (
        value.get("summary", {}).get("status") != "ok"
        or not isinstance(value.get("samples"), list)
        or not value["samples"]
    ):
        raise ClassifierError("candidate dataset manifest must contain successful non-empty samples")
    if not isinstance(value.get("sources"), list) or not value["sources"]:
        raise ClassifierError("candidate dataset manifest must contain sources")
    if not isinstance(value.get("dataset_version"), str) or not value["dataset_version"]:
        raise ClassifierError("candidate dataset manifest requires dataset_version")
    _validate_dataset_tensor_contract(value.get("tensor_contract"))
    _unique_by(value["samples"], "candidate_id", "dataset samples")
    return path, value, sha256


def _verify_resolution_bindings(
    dataset: dict[str, Any],
    resolution: dict[str, Any],
    dataset_sha256: str,
    contract_sha256: str,
) -> None:
    contract_binding = resolution.get("source_contract")
    dataset_contract = dataset.get("contract")
    if not isinstance(dataset_contract, dict) or not isinstance(dataset_contract.get("sha256"), str):
        raise ClassifierError("candidate dataset manifest lacks its source contract sha256")
    if not isinstance(contract_binding, dict) or contract_binding.get("sha256") != dataset_contract["sha256"]:
        raise ClassifierError("annotation resolution source_contract sha256 does not match the dataset source contract")
    dataset_binding = resolution.get("source_dataset_manifest")
    if isinstance(dataset_binding, dict):
        bound_sha256 = dataset_binding.get("sha256")
        bound_version = dataset_binding.get("dataset_version")
    else:
        ledger_binding = resolution.get("source_vote_ledger")
        if not isinstance(ledger_binding, dict):
            raise ClassifierError("annotation resolution lacks a cryptographic dataset binding")
        bound_sha256 = ledger_binding.get("evidence_manifest_sha256")
        bound_version = ledger_binding.get("dataset_version")
    if bound_sha256 != dataset_sha256:
        raise ClassifierError("annotation resolution dataset manifest sha256 mismatch")
    if bound_version != dataset.get("dataset_version"):
        raise ClassifierError("annotation resolution dataset_version mismatch")
    derived_binding = resolution.get("derived_tracking_contract")
    if not isinstance(derived_binding, dict) or derived_binding.get("sha256") != contract_sha256:
        raise ClassifierError("annotation resolution derived_tracking_contract sha256 mismatch")


def _validate_candidate_identities(
    samples: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    sources: list[dict[str, Any]],
) -> dict[str, str]:
    candidate_by_id = _unique_by(candidates, "candidate_id", "contract candidates")
    source_by_variant = _unique_by(sources, "variant_id", "dataset sources")
    fingerprints = {}
    for sample in samples:
        candidate_id = sample["candidate_id"]
        candidate = candidate_by_id[candidate_id]
        source = source_by_variant.get(sample.get("variant_id"))
        if source is None:
            raise ClassifierError(f"sample {candidate_id!r} references absent source variant")
        sample_bbox = sample.get("bbox_requested_pixels")
        if not isinstance(sample_bbox, list) or len(sample_bbox) != 4:
            raise ClassifierError(f"sample {candidate_id!r} lacks requested bbox identity")
        sample_identity = {
            "candidate_id": candidate_id,
            "frame_index": sample.get("frame_index"),
            "bbox": [_finite_number(value, "sample bbox") for value in sample_bbox],
            "detector_source": sample.get("detector_source"),
        }
        contract_identity = {
            "candidate_id": candidate_id,
            "frame_index": candidate.get("frame_index"),
            "bbox": [_finite_number(value, "contract bbox") for value in candidate.get("bbox", [])],
            "detector_source": candidate.get("source"),
            "confidence": _finite_number(candidate.get("confidence"), "contract confidence"),
        }
        sample_identity["confidence"] = _finite_number(sample.get("confidence"), "sample confidence")
        if sample_identity != contract_identity:
            raise ClassifierError(f"dataset/contract candidate identity mismatch for {candidate_id!r}")
        width = _positive_finite(source.get("width"), "source width")
        height = _positive_finite(source.get("height"), "source height")
        clamped_bbox = sample.get("bbox_clamped_pixels")
        if not isinstance(clamped_bbox, list) or len(clamped_bbox) != 4:
            raise ClassifierError(f"sample {candidate_id!r} lacks clamped bbox identity")
        clamped_values = [_finite_number(value, "clamped bbox") for value in clamped_bbox]
        expected_clamped = [
            min(width, max(0.0, contract_identity["bbox"][0])),
            min(height, max(0.0, contract_identity["bbox"][1])),
            min(width, max(0.0, contract_identity["bbox"][2])),
            min(height, max(0.0, contract_identity["bbox"][3])),
        ]
        if (
            clamped_values != expected_clamped
            or clamped_values[2] <= clamped_values[0]
            or clamped_values[3] <= clamped_values[1]
        ):
            raise ClassifierError(f"dataset/contract clamped bbox mismatch for {candidate_id!r}")
        expected_normalized = [
            clamped_values[0] / width,
            clamped_values[1] / height,
            clamped_values[2] / width,
            clamped_values[3] / height,
        ]
        observed_normalized = sample.get("bbox_normalized")
        if (
            not isinstance(observed_normalized, list)
            or len(observed_normalized) != 4
            or any(
                not math.isclose(_finite_number(observed, "normalized bbox"), expected, abs_tol=1e-8)
                for observed, expected in zip(observed_normalized, expected_normalized)
            )
        ):
            raise ClassifierError(f"dataset/contract normalized bbox mismatch for {candidate_id!r}")
        fingerprints[candidate_id] = _canonical_sha256(contract_identity)
    return fingerprints


def _windows_overlap(left: list[int], right: list[int]) -> bool:
    return max(left[0], right[0]) <= min(left[1], right[1])


def _load_resolution(path: Path) -> tuple[Path, dict[str, Any], str]:
    path = Path(path).resolve()
    sha256 = _sha256_file(path)
    value = _load_json_object(path, "annotation resolution")
    _verify_file_bindings({"annotation resolution": (path, sha256)})
    if value.get("schema_version") != "1.0" or value.get("artifact_type") != "candidate_annotation_resolution":
        raise ClassifierError("invalid annotation resolution envelope")
    if value.get("summary", {}).get("status") != "complete" or not isinstance(value.get("resolutions"), list):
        raise ClassifierError("annotation resolution is incomplete")
    return path, value, sha256


def _load_contract(path: Path) -> tuple[Path, dict[str, Any], str]:
    path = Path(path).resolve()
    sha256 = _sha256_file(path)
    value = load_tracking_contract(path)
    _verify_file_bindings({"tracking contract": (path, sha256)})
    if value.get("artifact_status") != "loaded" or value.get("validation_errors"):
        raise ClassifierError(f"invalid tracking contract: {value.get('validation_errors')}")
    return path, value, sha256


def _validated_config(config: TrainingConfig) -> TrainingConfig:
    if not isinstance(config.epochs, int) or isinstance(config.epochs, bool) or config.epochs <= 0:
        raise ClassifierError("epochs must be a positive integer")
    if not isinstance(config.batch_size, int) or isinstance(config.batch_size, bool) or config.batch_size <= 0:
        raise ClassifierError("batch_size must be a positive integer")
    if not math.isfinite(config.learning_rate) or config.learning_rate <= 0:
        raise ClassifierError("learning_rate must be finite and positive")
    if not math.isfinite(config.weight_decay) or config.weight_decay < 0:
        raise ClassifierError("weight_decay must be finite and non-negative")
    if not isinstance(config.seed, int) or isinstance(config.seed, bool) or config.seed < 0:
        raise ClassifierError("seed must be a non-negative integer")
    return config


def _set_deterministic_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(True)


def _require_match_and_noise(examples: list[dict[str, Any]], name: str) -> None:
    labels = {item["label"] for item in examples}
    if "match_ball" not in labels or not labels.intersection(_NOISE_LABELS):
        raise ClassifierError(f"{name} requires match_ball and at least one confirmed noise class")


def _validate_model_inputs(tight: torch.Tensor, context: torch.Tensor, metadata: torch.Tensor) -> None:
    if tight.ndim != 5 or tuple(tight.shape[1:]) != TIGHT_SHAPE:
        raise ClassifierError(f"tight tensor shape must be (batch, {TIGHT_SHAPE})")
    if context.ndim != 5 or tuple(context.shape[1:]) != CONTEXT_SHAPE:
        raise ClassifierError(f"context tensor shape must be (batch, {CONTEXT_SHAPE})")
    if metadata.ndim != 2 or metadata.shape[1] != METADATA_DIM:
        raise ClassifierError(f"metadata tensor shape must be (batch, {METADATA_DIM})")
    if tight.shape[0] != context.shape[0] or tight.shape[0] != metadata.shape[0]:
        raise ClassifierError("classifier batch dimensions must match")


def _architecture_contract(parameter_count: int) -> dict[str, Any]:
    return {
        "name": "dual_temporal_candidate_cnn_v1",
        "tight_branch_channels": [15, 8, 12],
        "context_branch_channels": [15, 8, 12],
        "metadata_dim": METADATA_DIM,
        "hidden_dim": 32,
        "output_dim": len(CLASS_LABELS),
        "parameter_count": parameter_count,
        "parameter_budget": 100_000,
    }


def _input_contract(dataset: dict[str, Any]) -> dict[str, Any]:
    return {
        "tight_shape": list(TIGHT_SHAPE),
        "context_shape": list(CONTEXT_SHAPE),
        "metadata_order": ["x1", "y1", "x2", "y2", "center_x", "center_y", "confidence"],
        "image_dtype": "uint8",
        "model_image_scale": "divide_by_255",
        "color_space": "RGB",
        "dataset_tensor_contract": dataset["tensor_contract"],
        "semantic_preprocessing": {
            "frame_offsets": dataset.get("frame_offsets"),
            "tensor_contract": dataset["tensor_contract"],
            "preprocessing_runtime": dataset.get("preprocessing_runtime"),
        },
    }


def _validate_input_contract(value: Any) -> None:
    if not isinstance(value, dict):
        raise ClassifierError("model input contract must be an object")
    if value.get("tight_shape") != list(TIGHT_SHAPE) or value.get("context_shape") != list(CONTEXT_SHAPE):
        raise ClassifierError("model input shape contract mismatch")
    if value.get("metadata_order") != ["x1", "y1", "x2", "y2", "center_x", "center_y", "confidence"]:
        raise ClassifierError("model metadata contract mismatch")
    _validate_dataset_tensor_contract(value.get("dataset_tensor_contract"))
    semantic = value.get("semantic_preprocessing")
    if not isinstance(semantic, dict):
        raise ClassifierError("model semantic preprocessing contract must be an object")
    if semantic.get("frame_offsets") != [-2, -1, 0, 1, 2]:
        raise ClassifierError("model frame offset contract mismatch")
    if semantic.get("tensor_contract") != value.get("dataset_tensor_contract"):
        raise ClassifierError("model tensor preprocessing fields are inconsistent")
    runtime = semantic.get("preprocessing_runtime")
    required_runtime_fields = {
        "pipeline",
        "frame_offsets",
        "tight_crop_scale",
        "context_crop_scale",
        "tight_size",
        "context_size",
        "color_conversion",
        "resize_down",
        "resize_up",
        "python",
        "numpy",
        "opencv",
    }
    if not isinstance(runtime, dict) or not required_runtime_fields.issubset(runtime):
        raise ClassifierError("model preprocessing runtime fingerprint is incomplete")


def _validate_dataset_tensor_contract(value: Any) -> None:
    expected = {
        "color_space": "RGB",
        "dtype": "uint8",
        "tight_shape": list(TIGHT_SHAPE),
        "context_shape": list(CONTEXT_SHAPE),
        "markup": False,
    }
    if value != expected:
        raise ClassifierError("dataset tensor contract mismatch")


def _validate_inference_preprocessing(dataset: dict[str, Any], manifest: dict[str, Any]) -> None:
    observed = {
        "frame_offsets": dataset.get("frame_offsets"),
        "tensor_contract": dataset.get("tensor_contract"),
        "preprocessing_runtime": dataset.get("preprocessing_runtime"),
    }
    if observed != manifest.get("input_contract", {}).get("semantic_preprocessing"):
        raise ClassifierError("inference dataset tensor preprocessing does not match the model")


def _runtime_fingerprint() -> dict[str, Any]:
    return {
        "device": "cpu",
        "python": platform.python_version(),
        "torch": torch.__version__,
        "numpy": np.__version__,
    }


def _validated_supported_mask(value: Any) -> list[bool]:
    if (
        not isinstance(value, list)
        or len(value) != len(CLASS_LABELS)
        or not all(isinstance(item, bool) for item in value)
    ):
        raise ClassifierError("supported class mask must contain seven booleans")
    if not value[CLASS_LABELS.index("match_ball")] or not any(
        value[CLASS_LABELS.index(label)] for label in _NOISE_LABELS
    ):
        raise ClassifierError("supported class mask requires match_ball and noise")
    return value


def _unique_by(values: Any, field: str, name: str) -> dict[str, dict[str, Any]]:
    if not isinstance(values, list):
        raise ClassifierError(f"{name} must be a list")
    result = {}
    for index, item in enumerate(values):
        if not isinstance(item, dict) or not isinstance(item.get(field), str) or not item[field]:
            raise ClassifierError(f"{name}[{index}].{field} must be a non-empty string")
        if item[field] in result:
            raise ClassifierError(f"{name} contains duplicate {field} {item[field]!r}")
        result[item[field]] = item
    return result


def _load_json_object(path: Path, name: str) -> dict[str, Any]:
    if not path.is_file():
        raise ClassifierError(f"{name} is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ClassifierError(f"{name} is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise ClassifierError(f"{name} must be an object")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _required_sha256(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ClassifierError(f"{name} must be a lowercase sha256 digest")
    return value


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ClassifierError(f"{name} must be a non-empty string")
    return value.strip()


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ClassifierError(f"{name} must be finite")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ClassifierError(f"{name} must be finite") from exc
    if not math.isfinite(parsed):
        raise ClassifierError(f"{name} must be finite")
    return parsed


def _positive_finite(value: Any, name: str) -> float:
    parsed = _finite_number(value, name)
    if parsed <= 0:
        raise ClassifierError(f"{name} must be positive")
    return parsed


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _write_json_file(path: Path, value: dict[str, Any]) -> None:
    path.write_bytes(_json_bytes(value))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ClassifierError(f"could not hash artifact {path}: {exc}") from exc
    return digest.hexdigest()


def _verify_file_bindings(bindings: dict[str, tuple[Path, str]]) -> None:
    for name, (path, expected_sha256) in bindings.items():
        try:
            actual_sha256 = _sha256_file(path)
        except ClassifierError as exc:
            raise ClassifierError(f"{name} changed during classifier operation: {exc}") from exc
        if actual_sha256 != expected_sha256:
            raise ClassifierError(f"{name} changed during classifier operation")


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ClassifierError(f"argument error: {message}")


def train_cli_main(argv: list[str] | None = None) -> int:
    parser = _JsonArgumentParser(description="Train the CPU candidate classifier.")
    parser.add_argument("--dataset-manifest", required=True, type=Path)
    parser.add_argument("--annotation-resolution", required=True, type=Path)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--learning-rate", type=float, default=0.005)
    parser.add_argument("--weight-decay", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=1337)
    try:
        args = parser.parse_args(argv)
        report = train_candidate_classifier(
            args.dataset_manifest,
            args.annotation_resolution,
            args.contract,
            args.output_dir,
            config=TrainingConfig(
                epochs=args.epochs,
                batch_size=args.batch_size,
                learning_rate=args.learning_rate,
                weight_decay=args.weight_decay,
                seed=args.seed,
            ),
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "status": report["status"], "model_version": report["model_version"]}))
    return 0


def classify_cli_main(argv: list[str] | None = None) -> int:
    parser = _JsonArgumentParser(description="Run CPU candidate classification.")
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--dataset-manifest", required=True, type=Path)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--batch-size", type=int, default=32)
    try:
        args = parser.parse_args(argv)
        report = classify_candidates(
            args.package,
            args.dataset_manifest,
            args.contract,
            args.output_dir,
            batch_size=args.batch_size,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "model_version": report["model_version"],
                "prediction_count": report["prediction_count"],
            }
        )
    )
    return 0
