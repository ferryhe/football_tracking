from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from football_tracking.detector_development_common import (
    DetectorDevelopmentError,
    atomic_write_json,
    canonical_sha256,
    hash_regular_file,
    is_link_or_reparse,
    json_object_from_bytes,
    read_regular_bytes,
    regular_file_identity,
    secure_mkdirs,
    snapshot_identity_is_current,
    stat_token,
)

REGISTRY_SCHEMA_VERSION = "1.0"
REGISTRY_ARTIFACT_TYPE = "ball_detector_development_v1"
_RUNTIME_CONTRACT = {
    "ultralytics": ">=8.3.0,<9",
    "sahi": ">=0.11.22,<1",
    "torch": ">=2,<3",
}
_RUNTIME_BOUNDS = {
    "ultralytics": ((8, 3, 0), (9, 0, 0)),
    "sahi": ((0, 11, 22), (1, 0, 0)),
    "torch": ((2, 0, 0), (3, 0, 0)),
}
_STRICT_RUNTIME_VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?(?:\.(0|[1-9][0-9]*))?"
    r"(?:\+[0-9A-Za-z]+(?:[._-][0-9A-Za-z]+)*)?$"
)

_COCO_CLASS_NAMES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich",
    "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book",
    "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
)

_BUILTIN_MODELS: tuple[dict[str, Any], ...] = (
    {
        "model_id": "current-coco-yolov8n",
        "version": "yolov8n-coco-2022-12-30",
        "model_version": "8.0.0",
        "display_name": "Current COCO YOLOv8n baseline",
        "architecture_family": "yolov8",
        "weight_name": "football_ball_yolo.pt",
        "weight_sha256": "f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36",
        "weight_size": 6_549_796,
        "asset_release": "workspace-baseline-2022-12-30",
        "weight_url": "local-workspace://weights/football_ball_yolo.pt",
        "checkpoint_format_version": "8.0.0.dev0",
        "checkpoint_date": "2022-12-30",
    },
    {
        "model_id": "official-coco-yolo11n",
        "version": "yolo11n-coco-v8.4.0",
        "model_version": "11.0.0",
        "display_name": "Official COCO YOLO11n",
        "architecture_family": "yolo11",
        "weight_name": "yolo11n.pt",
        "weight_sha256": "0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1",
        "weight_size": 5_613_764,
        "asset_release": "v8.4.0",
        "weight_url": "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.pt",
        "checkpoint_format_version": "8.2.100",
        "checkpoint_date": "2024-09-25",
    },
    {
        "model_id": "official-coco-yolo11s",
        "version": "yolo11s-coco-v8.4.0",
        "model_version": "11.0.0",
        "display_name": "Official COCO YOLO11s",
        "architecture_family": "yolo11",
        "weight_name": "yolo11s.pt",
        "weight_sha256": "85a76fe86dd8afe384648546b56a7a78580c7cb7b404fc595f97969322d502d5",
        "weight_size": 19_313_732,
        "asset_release": "v8.4.0",
        "weight_url": "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11s.pt",
        "checkpoint_format_version": "8.2.100",
        "checkpoint_date": "2024-09-25",
    },
)

_LICENSES = {
    "dataset": {
        "name": "COCO dataset terms",
        "spdx_id": "LicenseRef-COCO-Dataset-Terms",
        "url": "https://cocodataset.org/#termsofuse",
        "reviewed": True,
        "approved_for_local_probe": True,
    },
    "model": {
        "name": "Ultralytics model license",
        "spdx_id": "AGPL-3.0-only",
        "url": "https://www.ultralytics.com/license",
        "reviewed": True,
        "approved_for_local_probe": True,
    },
    "runtime": {
        "name": "Ultralytics AGPL-3.0; SAHI MIT; Torch BSD-3-Clause",
        "spdx_id": "LicenseRef-Mixed-Runtime-Licenses",
        "url": "https://docs.ultralytics.com/",
        "reviewed": True,
        "approved_for_local_probe": True,
    },
    "deployment": {
        "name": "Local evaluation only; deployment license review required",
        "spdx_id": "LicenseRef-Deployment-Review-Required",
        "url": "https://www.ultralytics.com/license",
        "reviewed": True,
        "approved_for_local_probe": True,
    },
}


def build_builtin_model_catalog(
    repo_root: Path,
    *,
    load_observer: Callable[[Path, str, dict[str, str | None]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return immutable descriptors plus a fresh, independent availability observation."""

    root = Path(repo_root).resolve(strict=True)
    runtime = _runtime_metadata()
    models: list[dict[str, Any]] = []
    profiles: list[dict[str, Any]] = []
    for spec in _BUILTIN_MODELS:
        descriptor = _build_descriptor(root, spec, runtime)
        availability, load = _observe_builtin_availability(
            root / "weights" / spec["weight_name"],
            descriptor,
            runtime,
            load_observer or (lambda path, digest, current: _persisted_load_observation(root, path, digest, current)),
        )
        model = {
            "descriptor": descriptor,
            "availability": availability,
            "qualification": {
                "trial_eligible": False,
                "source_segment_qualified": False,
                "camera_qualified": False,
            },
            "selectable_for_probe": availability["status"] == "available",
        }
        models.append(model)
        profiles.extend(_build_profiles(descriptor, availability, load, runtime))
    return deepcopy(
        {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "artifact_type": REGISTRY_ARTIFACT_TYPE,
            "models": models,
            "profiles": profiles,
            "catalog_findings": _public_catalog_findings(),
        }
    )


def builtin_model_identities() -> frozenset[tuple[str, str]]:
    return frozenset((str(spec["model_id"]), str(spec["version"])) for spec in _BUILTIN_MODELS)


def find_model_and_profile(catalog: dict[str, Any], profile_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    profile = next((item for item in catalog.get("profiles", []) if item.get("profile_id") == profile_id), None)
    if profile is None:
        raise DetectorDevelopmentError("unknown_profile", f"Unknown detector profile: {profile_id}", status_code=400)
    model = next(
        (
            item for item in catalog.get("models", [])
            if item.get("descriptor", {}).get("model_id") == profile.get("model_id")
            and item.get("descriptor", {}).get("version") == profile.get("model_version")
        ),
        None,
    )
    if model is None:
        raise DetectorDevelopmentError("invalid_registry", f"Detector profile has no exact model: {profile_id}")
    return deepcopy(model), deepcopy(profile)


def _build_descriptor(repo_root: Path, spec: dict[str, Any], runtime: dict[str, str | None]) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "artifact_type": "detector_model_descriptor",
        "model_id": spec["model_id"],
        "version": spec["version"],
        "model_version": spec["model_version"],
        "display_name": spec["display_name"],
        "architecture_family": spec["architecture_family"],
        "weights": {
            "relative_path": f"weights/{spec['weight_name']}",
            "sha256": spec["weight_sha256"],
            "size_bytes": spec["weight_size"],
        },
        "source": {
            "project": "Ultralytics COCO detection",
            "version": spec["model_version"],
            "asset_release": spec["asset_release"],
            "weight_url": spec["weight_url"],
            "acquisition_method": "pinned_local_asset",
            "access_requirement": "pinned_local_file",
        },
        "checkpoint": {
            "format_version": spec["checkpoint_format_version"],
            "created_date": spec["checkpoint_date"],
        },
        "runtime_contract": deepcopy(_RUNTIME_CONTRACT),
        "class_names": list(_COCO_CLASS_NAMES),
        "class_map": {"sports ball": "ball"},
        "expected_input": {
            "direct_image_size": 1280,
            "sahi_slice_width": 1280,
            "sahi_slice_height": 720,
            "source_coordinate_space": "source_pixels_xyxy",
        },
        "execution": {
            "device": "auto",
            "precision": "fp32",
            "memory_envelope": {"max_ram_mb": 8192, "max_vram_mb": 8192},
        },
        "licenses": deepcopy(_LICENSES),
        "egress": {
            "frames_leave_local_machine": False,
            "destination": None,
            "operator_consent": "not_required",
        },
        "lifecycle_state": "unverified",
        "bindings": {
            "source_sha256": None,
            "temporal_group_sha256": None,
            "camera_profile_sha256": None,
            "evaluation_package_sha256": None,
            "threshold_profile_sha256": None,
            "code_commit": None,
            "environment_sha256": None,
        },
    }
    descriptor["descriptor_sha256"] = canonical_sha256(descriptor)
    return descriptor


def _observe_builtin_availability(
    path: Path,
    descriptor: dict[str, Any],
    runtime: dict[str, str | None],
    load_observer: Callable[[Path, str, dict[str, str | None]], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected_digest = descriptor["weights"]["sha256"]
    expected_size = descriptor["weights"]["size_bytes"]
    observations: dict[str, dict[str, Any]] = {
        "file": {"status": "fail", "reason": "weights_missing"},
        "digest": {"status": "not_run", "reason": "weights_missing"},
        "class_map": {"status": "not_run", "reason": "runtime_load_evidence_missing"},
        "license": {"status": "pass", "reason": "local_probe_license_metadata_complete"},
        "runtime_load": {
            "status": "not_run",
            "reason": "weights_missing",
            "installed_runtime": runtime,
        },
    }
    load: dict[str, Any] = {"direct": False, "sahi": False, "class_names": []}
    reason_codes: list[str] = []
    if not path.is_file() or is_link_or_reparse(path):
        reason_codes.append("weights_missing_or_unsafe")
    else:
        try:
            identity = regular_file_identity(path, "built-in detector weights")
        except DetectorDevelopmentError as exc:
            observations["file"] = {"status": "fail", "reason": exc.code}
            reason_codes.append(exc.code)
        else:
            if identity[2] != expected_size:
                observations["file"] = {
                    "status": "fail",
                    "reason": "weights_digest_or_size_mismatch",
                }
                reason_codes.append("weights_digest_or_size_mismatch")
                actual_digest = None
                actual_size = None
            else:
                observations["file"] = {"status": "pass", "reason": "regular_nonlink_file"}
                try:
                    actual_digest, actual_size = hash_regular_file(
                        path,
                        "built-in detector weights",
                        max_bytes=expected_size,
                        trusted_root=path.parent,
                    )
                except DetectorDevelopmentError as exc:
                    observations["digest"] = {"status": "fail", "reason": exc.code}
                    reason_codes.append(exc.code)
                    actual_digest = None
                    actual_size = None
            if actual_digest == expected_digest and actual_size == expected_size:
                observations["digest"] = {"status": "pass", "reason": "pinned_bytes_match"}
                if not _runtime_contract_satisfied(runtime, descriptor["runtime_contract"]):
                    observations["runtime_load"] = {
                        "status": "fail",
                        "reason": "runtime_contract_mismatch",
                        "installed_runtime": runtime,
                        "evidence_sha256": None,
                    }
                    reason_codes.append("runtime_contract_mismatch")
                else:
                    load = load_observer(path, actual_digest, runtime)
                evidence_sha256 = load.get("evidence_sha256")
                evidence_is_bound = (
                    isinstance(evidence_sha256, str)
                    and re.fullmatch(r"[0-9a-f]{64}", evidence_sha256) is not None
                )
                if (
                    observations["runtime_load"]["reason"] != "runtime_contract_mismatch"
                    and load["direct"]
                    and load["sahi"]
                    and "sports ball" in load["class_names"]
                    and evidence_is_bound
                ):
                    observations["runtime_load"] = {
                        "status": "pass",
                        "reason": "pinned_direct_and_sahi_load_smoke_passed",
                        "installed_runtime": runtime,
                        "evidence_sha256": evidence_sha256,
                    }
                    observations["class_map"] = {
                        "status": "pass",
                        "reason": "checkpoint_sports_ball_maps_to_ball",
                    }
                elif (
                    observations["runtime_load"]["reason"] != "runtime_contract_mismatch"
                    and load.get("reason") == "runtime_load_evidence_missing"
                ):
                    observations["runtime_load"] = {
                        "status": "not_run",
                        "reason": "runtime_load_check_required",
                        "installed_runtime": runtime,
                        "evidence_sha256": None,
                    }
                    reason_codes.extend(("runtime_load_check_required", "class_map_check_required"))
                elif observations["runtime_load"]["reason"] != "runtime_contract_mismatch":
                    observations["runtime_load"] = {
                        "status": "fail",
                        "reason": (
                            load.get("reason")
                            or (
                                "runtime_load_evidence_invalid"
                                if not evidence_is_bound
                                else "runtime_load_failed"
                            )
                        ),
                        "installed_runtime": runtime,
                        "evidence_sha256": evidence_sha256,
                    }
                    reason_codes.append("runtime_load_failed")
                    if load.get("direct") is True and load.get("sahi") is True and "sports ball" not in load.get("class_names", []):
                        observations["class_map"] = {"status": "fail", "reason": "checkpoint_class_map_mismatch"}
                        reason_codes.append("class_map_mismatch")
            else:
                if actual_digest is not None:
                    observations["digest"] = {"status": "fail", "reason": "pinned_bytes_mismatch"}
                    reason_codes.append("weights_digest_or_size_mismatch")
    status = "available" if all(item["status"] == "pass" for item in observations.values()) else "unavailable"
    return {
        "status": status,
        "reason_codes": sorted(set(reason_codes)),
        "observations": observations,
        "observed_weight_path": descriptor["weights"]["relative_path"],
    }, load


def _runtime_contract_satisfied(
    runtime: dict[str, str | None],
    contract: dict[str, str],
) -> bool:
    if contract != _RUNTIME_CONTRACT:
        return False
    for name, (lower, upper) in _RUNTIME_BOUNDS.items():
        installed = runtime.get(name)
        if not isinstance(installed, str) or not installed:
            return False
        match = _STRICT_RUNTIME_VERSION_RE.fullmatch(installed)
        if match is None:
            return False
        release = tuple(int(item or 0) for item in match.groups())
        if not lower <= release < upper:
            return False
    return True


def _persisted_load_observation(
    repo_root: Path,
    weight_path: Path,
    digest: str,
    runtime: dict[str, str | None],
) -> dict[str, Any]:
    spec = next((item for item in _BUILTIN_MODELS if item["weight_name"] == weight_path.name), None)
    if spec is None:
        return {"direct": False, "sahi": False, "class_names": [], "reason": "unrecognized_builtin_weight"}
    contract_sha256 = canonical_sha256(_RUNTIME_CONTRACT)
    evidence_path = _runtime_observation_path(repo_root, spec["model_id"], digest, contract_sha256)
    if not evidence_path.is_file() or is_link_or_reparse(evidence_path):
        return {
            "direct": False,
            "sahi": False,
            "class_names": [],
            "reason": "runtime_load_evidence_missing",
        }
    try:
        content, evidence_sha256 = read_regular_bytes(
            evidence_path,
            "detector runtime-load evidence",
            max_bytes=256 * 1024,
            trusted_root=evidence_path.parent,
        )
        payload = json_object_from_bytes(content, "detector runtime-load evidence")
    except DetectorDevelopmentError as exc:
        return {"direct": False, "sahi": False, "class_names": [], "reason": exc.code}
    expected = {
        "artifact_type": "detector_runtime_load_observation",
        "schema_version": "1.0",
        "weights_sha256": digest,
        "weights_size_bytes": weight_path.stat().st_size,
        "runtime": runtime,
        "runtime_contract_sha256": contract_sha256,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        return {"direct": False, "sahi": False, "class_names": [], "reason": "runtime_load_evidence_mismatch"}
    return {
        "direct": payload.get("direct_load_passed") is True,
        "sahi": payload.get("sahi_load_passed") is True,
        "class_names": [str(item) for item in payload.get("class_names", [])],
        "reason": None,
        "evidence_sha256": evidence_sha256,
    }


def observe_pinned_model_runtime(repo_root: Path, model_id: str, *, timeout_seconds: int = 120) -> dict[str, Any]:
    """Explicitly smoke-test one exact built-in weight; never accepts an imported path."""

    spec = next((item for item in _BUILTIN_MODELS if item["model_id"] == model_id), None)
    if spec is None:
        raise DetectorDevelopmentError("unknown_builtin_model", f"Unknown pinned detector model: {model_id}", status_code=400)
    root = Path(repo_root).resolve(strict=True)
    weights_root = root / "weights"
    path = weights_root / spec["weight_name"]
    development_root = secure_mkdirs(root, "data", "ball_detector_development_v1")
    observation_root = secure_mkdirs(development_root, "model_observations")
    snapshot_root = Path(
        tempfile.mkdtemp(prefix=f".{model_id}.runtime-snapshot-", dir=observation_root)
    )
    snapshot_path = snapshot_root / spec["weight_name"]
    source_identity: tuple[int, int, int, int, int] | None = None
    snapshot_identity: tuple[int, int, int, int, int] | None = None
    try:
        actual_sha256, actual_size, source_identity, snapshot_identity = (
            _copy_pinned_runtime_snapshot(
                path,
                snapshot_path,
                expected_sha256=spec["weight_sha256"],
                expected_size=spec["weight_size"],
            )
        )

        runtime = _runtime_metadata()
        contract_sha256 = canonical_sha256(_RUNTIME_CONTRACT)
        script = r'''
import json
import sys
import numpy as np
from ultralytics import YOLO
from sahi import AutoDetectionModel
from sahi.predict import get_sliced_prediction

path = sys.argv[1]
image = np.zeros((64, 64, 3), dtype=np.uint8)
direct_model = YOLO(path)
names = direct_model.names
class_names = [str(names[index]) for index in sorted(names)] if isinstance(names, dict) else [str(item) for item in names]
direct_model.predict(image, imgsz=64, conf=0.99, device="cpu", half=False, verbose=False)
sahi_model = AutoDetectionModel.from_pretrained(
    model_type="ultralytics", model_path=path, confidence_threshold=0.99, device="cpu", image_size=64
)
get_sliced_prediction(
    image=image, detection_model=sahi_model, slice_height=64, slice_width=64,
    overlap_height_ratio=0.0, overlap_width_ratio=0.0, perform_standard_pred=False, verbose=0,
)
print("DETECTOR_RUNTIME_OBSERVATION=" + json.dumps({
    "direct_load_passed": True, "sahi_load_passed": True, "class_names": class_names
}))
'''
        failure: str | None = None
        direct_passed = False
        sahi_passed = False
        class_names: list[str] = []
        try:
            completed = subprocess.run(
                [sys.executable, "-c", script, str(snapshot_path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=max(1, min(int(timeout_seconds), 300)),
                env={**os.environ, "PYTHONNOUSERSITE": "1"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failure = type(exc).__name__
        else:
            marker = "DETECTOR_RUNTIME_OBSERVATION="
            line = next((item for item in reversed(completed.stdout.splitlines()) if item.startswith(marker)), None)
            if completed.returncode == 0 and line is not None:
                try:
                    observed = json.loads(line[len(marker):])
                except json.JSONDecodeError:
                    failure = "invalid_observation_output"
                else:
                    direct_passed = observed.get("direct_load_passed") is True
                    sahi_passed = observed.get("sahi_load_passed") is True
                    class_names = [str(item) for item in observed.get("class_names", [])]
            else:
                failure = (completed.stderr or completed.stdout or "runtime_load_failed").strip()[-2000:]

        _verify_runtime_snapshot_and_source(
            path,
            snapshot_path,
            source_identity=source_identity,
            snapshot_identity=snapshot_identity,
            expected_sha256=actual_sha256,
            expected_size=actual_size,
            weights_root=weights_root,
            snapshot_root=snapshot_root,
        )
        payload = {
            "artifact_type": "detector_runtime_load_observation",
            "schema_version": "1.0",
            "model_id": model_id,
            "weights_sha256": actual_sha256,
            "weights_size_bytes": actual_size,
            "runtime_contract_sha256": contract_sha256,
            "runtime": runtime,
            "direct_load_passed": direct_passed,
            "sahi_load_passed": sahi_passed,
            "class_names": class_names,
            "failure": failure,
        }
        evidence_path = observation_root / f"{model_id}-{actual_sha256}-{contract_sha256}.json"
        atomic_write_json(evidence_path, payload, trusted_root=development_root)
        _, evidence_sha256 = read_regular_bytes(
            evidence_path,
            "detector runtime-load evidence",
            max_bytes=256 * 1024,
            trusted_root=evidence_path.parent,
        )
        return {**payload, "relative_path": evidence_path.relative_to(root).as_posix(), "evidence_sha256": evidence_sha256}
    finally:
        _cleanup_runtime_snapshot(snapshot_root, snapshot_path, snapshot_identity)


def _copy_pinned_runtime_snapshot(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> tuple[str, int, tuple[int, int, int, int, int], tuple[int, int, int, int, int]]:
    source_identity = regular_file_identity(source, "pinned detector weights")
    if source_identity[2] != expected_size:
        raise DetectorDevelopmentError(
            "weights_digest_or_size_mismatch",
            "Pinned detector bytes do not match the registry",
        )
    digest = hashlib.sha256()
    copied = 0
    try:
        with source.open("rb") as source_handle, destination.open("xb") as destination_handle:
            if stat_token(os.fstat(source_handle.fileno())) != source_identity:
                raise DetectorDevelopmentError("source_changed", "Pinned detector weights changed while opened")
            while True:
                chunk = source_handle.read(min(1024 * 1024, expected_size - copied + 1))
                if not chunk:
                    break
                copied += len(chunk)
                if copied > expected_size:
                    raise DetectorDevelopmentError(
                        "weights_digest_or_size_mismatch",
                        "Pinned detector bytes exceed the registry size",
                    )
                digest.update(chunk)
                destination_handle.write(chunk)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
            if stat_token(os.fstat(source_handle.fileno())) != source_identity:
                raise DetectorDevelopmentError("source_changed", "Pinned detector weights changed while copied")
    except DetectorDevelopmentError:
        raise
    except OSError as exc:
        raise DetectorDevelopmentError("copy_failed", "Pinned detector snapshot could not be created") from exc
    actual_sha256 = digest.hexdigest()
    if copied != expected_size or actual_sha256 != expected_sha256:
        raise DetectorDevelopmentError(
            "weights_digest_or_size_mismatch",
            "Pinned detector bytes do not match the registry",
        )
    if not snapshot_identity_is_current(source, source_identity):
        raise DetectorDevelopmentError("source_changed", "Pinned detector weights changed during snapshot")
    snapshot_sha256, snapshot_size = hash_regular_file(
        destination,
        "pinned detector runtime snapshot",
        max_bytes=expected_size,
        trusted_root=destination.parent,
    )
    if snapshot_sha256 != expected_sha256 or snapshot_size != expected_size:
        raise DetectorDevelopmentError("weights_snapshot_changed", "Pinned detector snapshot is invalid")
    snapshot_identity = regular_file_identity(destination, "pinned detector runtime snapshot")
    return actual_sha256, copied, source_identity, snapshot_identity


def _verify_runtime_snapshot_and_source(
    source: Path,
    snapshot: Path,
    *,
    source_identity: tuple[int, int, int, int, int],
    snapshot_identity: tuple[int, int, int, int, int],
    expected_sha256: str,
    expected_size: int,
    weights_root: Path,
    snapshot_root: Path,
) -> None:
    if not snapshot_identity_is_current(source, source_identity):
        raise DetectorDevelopmentError("source_changed", "Pinned detector weights changed during runtime smoke")
    if not snapshot_identity_is_current(snapshot, snapshot_identity):
        raise DetectorDevelopmentError("weights_snapshot_changed", "Pinned detector snapshot changed during runtime smoke")
    for candidate, label, trusted_root in (
        (source, "pinned detector weights", weights_root),
        (snapshot, "pinned detector runtime snapshot", snapshot_root),
    ):
        digest, size = hash_regular_file(
            candidate,
            label,
            max_bytes=expected_size,
            trusted_root=trusted_root,
        )
        if digest != expected_sha256 or size != expected_size:
            raise DetectorDevelopmentError("source_changed", f"{label} changed during runtime smoke")


def _cleanup_runtime_snapshot(
    root: Path,
    snapshot: Path,
    expected_snapshot_identity: tuple[int, int, int, int, int] | None,
) -> None:
    try:
        if expected_snapshot_identity is not None and snapshot_identity_is_current(
            snapshot, expected_snapshot_identity
        ):
            snapshot.unlink()
        root.rmdir()
    except OSError:
        pass


def _runtime_observation_path(repo_root: Path, model_id: str, digest: str, contract_sha256: str) -> Path:
    return (
        repo_root / "data" / "ball_detector_development_v1" / "model_observations"
        / f"{model_id}-{digest}-{contract_sha256}.json"
    )


def _build_profiles(
    descriptor: dict[str, Any],
    model_availability: dict[str, Any],
    load: dict[str, Any],
    runtime: dict[str, str | None],
) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for mode in ("direct", "sahi"):
        settings: dict[str, Any] = {
            "confidence_threshold": 0.05,
            "image_size": 1280,
            "use_half": False,
            "allowed_labels": ["sports ball"],
            "top_k": 5,
        }
        if mode == "sahi":
            settings.update(
                {
                    "slice_height": 720,
                    "slice_width": 1280,
                    "overlap_height_ratio": 0.20,
                    "overlap_width_ratio": 0.20,
                    "perform_standard_pred": False,
                    "postprocess_type": "NMS",
                    "postprocess_match_metric": "IOS",
                    "postprocess_match_threshold": 0.50,
                }
            )
        profile_base = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "artifact_type": "detector_profile",
            "profile_id": f"{descriptor['model_id']}-{mode}",
            "version": "1.0.0",
            "model_id": descriptor["model_id"],
            "model_version": descriptor["version"],
            "model_descriptor_sha256": descriptor["descriptor_sha256"],
            "mode": mode,
            "settings": settings,
        }
        profile = {**profile_base, "profile_sha256": canonical_sha256(profile_base)}
        profile["recommended"] = (
            (descriptor["model_id"] == "current-coco-yolov8n" and mode == "direct")
            or (descriptor["model_id"] == "official-coco-yolo11s" and mode == "sahi")
        )
        runtime_ok = load.get(mode) is True
        runtime_name = "ultralytics" if mode == "direct" else "sahi"
        installed = runtime.get(runtime_name)
        reason_codes = list(model_availability["reason_codes"])
        if not installed:
            reason_codes.append(f"{runtime_name}_runtime_missing")
        if not runtime_ok:
            reason_codes.append(f"{mode}_load_smoke_failed")
        profile_available = model_availability["status"] == "available" and bool(installed) and runtime_ok
        profile["availability"] = {
            "status": "available" if profile_available else "unavailable",
            "reason_codes": sorted(set(reason_codes)),
            "runtime": {"name": runtime_name, "installed_version": installed, "load_smoke": runtime_ok},
        }
        profile["selectable_for_probe"] = profile_available
        profiles.append(profile)
    return profiles


def _runtime_metadata() -> dict[str, str | None]:
    return {
        name: _installed_version(name)
        for name in ("ultralytics", "sahi", "torch")
    }


def _installed_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _public_catalog_findings() -> list[dict[str, Any]]:
    return [
        {
            "finding_id": "public-soccer-ball-yolo11n",
            "display_name": "Roboflow soccer-ball-detection-s2sg3 version 3",
            "source": {
                "project": "soccer-ball-detection-s2sg3",
                "version": "3",
                "url": "https://universe.roboflow.com/soccerdata-cnauk/soccer-ball-detection-s2sg3/model/3",
            },
            "architecture_family": "yolo11n",
            "access": {
                "method": "roboflow_hosted_or_account_export",
                "account_or_plan_required": "unverified",
                "local_weights_validated": False,
            },
            "licenses": _unreviewed_public_licenses(),
            "egress": {
                "frames_leave_local_machine": "unknown_until_access_method_selected",
                "destination": None,
                "operator_consent": "required_before_external_inference",
            },
            "selectable": False,
            "availability": {
                "status": "unavailable",
                "reason_codes": ["exact_weight_access_not_validated", "license_review_incomplete"],
            },
        },
        {
            "finding_id": "public-football-players-detection",
            "display_name": "Roboflow football-players-detection-3zvbc",
            "source": {
                "project": "football-players-detection-3zvbc",
                "version": None,
                "url": "https://universe.roboflow.com/roboflow-jvuqo/football-players-detection-3zvbc",
            },
            "architecture_family": "multiple_unbound_versions",
            "access": {
                "method": "unbound_project_version",
                "account_or_plan_required": "unverified",
                "local_weights_validated": False,
            },
            "licenses": _unreviewed_public_licenses(),
            "egress": {
                "frames_leave_local_machine": "unknown_until_access_method_selected",
                "destination": None,
                "operator_consent": "required_before_external_inference",
            },
            "selectable": False,
            "availability": {
                "status": "unavailable",
                "reason_codes": ["exact_version_not_selected", "exact_weight_access_not_validated", "license_review_incomplete"],
            },
        },
    ]


def _unreviewed_public_licenses() -> dict[str, dict[str, Any]]:
    return {
        kind: {
            "status": "review_required",
            "name": None,
            "spdx_id": None,
            "url": None,
            "approved_for_local_probe": False,
        }
        for kind in ("dataset", "model", "runtime", "deployment")
    }
