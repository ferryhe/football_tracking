from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from football_tracking.candidate_dataset import CandidateDatasetCancelled
from football_tracking.detector_development import (
    CorruptProbeFrameError,
    DetectorDevelopmentError,
    DetectorDevelopmentService,
    ProbeWorkerDiedError,
    build_builtin_model_catalog,
    merge_probe_candidates,
    normalize_probe_candidates,
)
from football_tracking.detector_development_common import canonical_sha256
from football_tracking.detector_probe_runner import (
    ArtifactWriteError,
    probe_execution_environment,
    run_detector_probe,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
EMPTY_TUNING_BINDING = {
    "state": "absent",
    "schema_version": "1.0",
    "version_id": None,
    "parent_version_id": None,
    "values_sha256": canonical_sha256({}),
}
EMPTY_TUNING_SHA256 = canonical_sha256(EMPTY_TUNING_BINDING)
_JPEG_FIXTURES: dict[tuple[int, int], bytes] = {}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _capture_thread_error(callback, errors: list[BaseException]) -> None:
    try:
        callback()
    except BaseException as exc:
        errors.append(exc)


def _pid_exists(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        kernel32.CloseHandle(handle)


def _terminate_pid(pid: int) -> None:
    if os.name != "nt":
        os.kill(pid, 9)
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x0001, False, pid)
    if not handle:
        return
    try:
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)


def _jpeg_fixture(width: int = 5120, height: int = 1440) -> bytes:
    cached = _JPEG_FIXTURES.get((width, height))
    if cached is not None:
        return cached
    import cv2
    import numpy as np

    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, ::64] = (0, 128, 0)
    encoded, payload = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not encoded:
        raise AssertionError("JPEG fixture could not be encoded")
    result = payload.tobytes()
    _JPEG_FIXTURES[(width, height)] = result
    return result


class DetectorCatalogTests(unittest.TestCase):
    def test_required_catalog_is_exact_versioned_and_has_six_independent_profiles(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        catalog = build_builtin_model_catalog(repo_root)

        self.assertEqual("ball_detector_development_v1", catalog["artifact_type"])
        self.assertEqual(
            {
                "current-coco-yolov8n",
                "official-coco-yolo11n",
                "official-coco-yolo11s",
            },
            {model["descriptor"]["model_id"] for model in catalog["models"]},
        )
        self.assertEqual(6, len(catalog["profiles"]))
        self.assertEqual(6, len({profile["profile_id"] for profile in catalog["profiles"]}))
        self.assertEqual({"direct", "sahi"}, {profile["mode"] for profile in catalog["profiles"]})
        self.assertTrue(all(profile["profile_sha256"] for profile in catalog["profiles"]))
        self.assertTrue(all(model["descriptor"]["descriptor_sha256"] for model in catalog["models"]))
        self.assertTrue(all(model["descriptor"]["lifecycle_state"] == "unverified" for model in catalog["models"]))
        self.assertTrue(
            all(
                model["qualification"]
                == {
                    "trial_eligible": False,
                    "source_segment_qualified": False,
                    "camera_qualified": False,
                }
                for model in catalog["models"]
            )
        )

        official_urls = {
            model["descriptor"]["source"]["weight_url"]
            for model in catalog["models"]
            if model["descriptor"]["model_id"].startswith("official-coco-yolo11")
        }
        self.assertEqual(
            {
                "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.pt",
                "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11s.pt",
            },
            official_urls,
        )
        self.assertTrue(all("latest" not in json.dumps(item).lower() for item in catalog["models"]))
        self.assertTrue(all(not finding["selectable"] for finding in catalog["catalog_findings"]))
        self.assertTrue(
            all(finding["availability"]["status"] == "unavailable" for finding in catalog["catalog_findings"])
        )
        exact_weights = {
            model["descriptor"]["model_id"]: (
                model["descriptor"]["weights"]["size_bytes"],
                model["descriptor"]["weights"]["sha256"],
            )
            for model in catalog["models"]
        }
        self.assertEqual(
            {
                "current-coco-yolov8n": (
                    6_549_796,
                    "f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36",
                ),
                "official-coco-yolo11n": (
                    5_613_764,
                    "0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1",
                ),
                "official-coco-yolo11s": (
                    19_313_732,
                    "85a76fe86dd8afe384648546b56a7a78580c7cb7b404fc595f97969322d502d5",
                ),
            },
            exact_weights,
        )
        official = next(
            model for model in catalog["models"] if model["descriptor"]["model_id"] == "official-coco-yolo11n"
        )
        self.assertEqual("11.0.0", official["descriptor"]["model_version"])
        self.assertEqual("v8.4.0", official["descriptor"]["source"]["asset_release"])
        self.assertEqual("8.2.100", official["descriptor"]["checkpoint"]["format_version"])
        installed_ultralytics = official["availability"]["observations"]["runtime_load"]["installed_runtime"][
            "ultralytics"
        ]
        self.assertTrue(installed_ultralytics is None or isinstance(installed_ultralytics, str))
        self.assertEqual(
            {"file", "digest", "class_map", "license", "runtime_load"},
            set(official["availability"]["observations"]),
        )

    def test_missing_one_weight_does_not_hide_an_available_model_or_direct_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo_root = Path(temporary)
            weights = repo_root / "weights"
            weights.mkdir()
            fixture = b"fixture-yolo11n"
            (weights / "yolo11n.pt").write_bytes(fixture)
            import football_tracking.detector_model_registry as registry

            specs = tuple(
                {
                    **spec,
                    "weight_sha256": hashlib.sha256(fixture).hexdigest(),
                    "weight_size": len(fixture),
                }
                if spec["model_id"] == "official-coco-yolo11n"
                else spec
                for spec in registry._BUILTIN_MODELS
            )
            with patch.object(registry, "_BUILTIN_MODELS", specs):
                catalog = build_builtin_model_catalog(
                    repo_root,
                    load_observer=lambda *_args: {
                        "direct": True,
                        "sahi": True,
                        "class_names": ["sports ball"],
                        "reason": None,
                        "evidence_sha256": SHA_A,
                    },
                )

        by_model = {item["descriptor"]["model_id"]: item for item in catalog["models"]}
        self.assertEqual("available", by_model["official-coco-yolo11n"]["availability"]["status"])
        self.assertEqual("unavailable", by_model["official-coco-yolo11s"]["availability"]["status"])
        by_profile = {item["profile_id"]: item for item in catalog["profiles"]}
        self.assertEqual("available", by_profile["official-coco-yolo11n-direct"]["availability"]["status"])
        self.assertEqual("unavailable", by_profile["official-coco-yolo11s-direct"]["availability"]["status"])

    def test_runtime_load_pass_cannot_omit_or_forge_its_evidence_digest(self) -> None:
        import football_tracking.detector_model_registry as registry

        for evidence_sha256 in (None, "not-a-digest", "A" * 64):
            with self.subTest(evidence_sha256=evidence_sha256), tempfile.TemporaryDirectory() as temporary:
                repo_root = Path(temporary)
                weights = repo_root / "weights"
                weights.mkdir()
                fixture = b"runtime-evidence-fixture"
                (weights / "fixture.pt").write_bytes(fixture)
                spec = {
                    **registry._BUILTIN_MODELS[1],
                    "weight_name": "fixture.pt",
                    "weight_sha256": hashlib.sha256(fixture).hexdigest(),
                    "weight_size": len(fixture),
                }
                with (
                    patch.object(registry, "_BUILTIN_MODELS", (spec,)),
                    patch.object(
                        registry,
                        "_runtime_metadata",
                        return_value={
                            "ultralytics": "8.4.31",
                            "sahi": "0.11.36",
                            "torch": "2.7.1+cpu",
                        },
                    ),
                ):
                    catalog = build_builtin_model_catalog(
                        repo_root,
                        load_observer=lambda *_args: {
                            "direct": True,
                            "sahi": True,
                            "class_names": ["sports ball"],
                            "reason": None,
                            "evidence_sha256": evidence_sha256,
                        },
                    )

                model = catalog["models"][0]
                self.assertEqual("unavailable", model["availability"]["status"])
                self.assertEqual(
                    "runtime_load_evidence_invalid",
                    model["availability"]["observations"]["runtime_load"]["reason"],
                )
                self.assertFalse(model["selectable_for_probe"])

    def test_catalog_responses_are_deeply_detached_from_immutable_registry_state(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        first = build_builtin_model_catalog(repo_root)
        first["models"][0]["descriptor"]["display_name"] = "tampered"
        first["profiles"][0]["settings"]["confidence_threshold"] = 0.99

        second = build_builtin_model_catalog(repo_root)

        self.assertNotEqual("tampered", second["models"][0]["descriptor"]["display_name"])
        self.assertNotEqual(0.99, second["profiles"][0]["settings"]["confidence_threshold"])

    def test_runtime_contract_rejects_each_below_above_and_malformed_runtime(self) -> None:
        import football_tracking.detector_model_registry as registry

        invalid_versions = {
            "ultralytics": ("8.2.99", "9.0.0", "9.0.0rc1", "not-a-version"),
            "sahi": ("0.11.21", "1.0.0", "not-a-version"),
            "torch": ("1.13.1", "3.0.0", "not-a-version"),
        }
        with tempfile.TemporaryDirectory() as temporary:
            repo_root = Path(temporary)
            weights_root = repo_root / "weights"
            weights_root.mkdir()
            payload = b"runtime-contract-fixture"
            (weights_root / "fixture.pt").write_bytes(payload)
            spec = {
                **registry._BUILTIN_MODELS[1],
                "weight_name": "fixture.pt",
                "weight_sha256": hashlib.sha256(payload).hexdigest(),
                "weight_size": len(payload),
            }
            compatible = {
                "ultralytics": "8.4.31",
                "sahi": "0.11.36",
                "torch": "2.7.1+cpu",
            }
            for runtime_name, versions in invalid_versions.items():
                for installed_version in versions:
                    runtime = {**compatible, runtime_name: installed_version}
                    observer = unittest.mock.Mock(
                        return_value={
                            "direct": True,
                            "sahi": True,
                            "class_names": ["sports ball"],
                            "evidence_sha256": SHA_A,
                        }
                    )
                    with (
                        self.subTest(runtime=runtime_name, version=installed_version),
                        patch.object(registry, "_BUILTIN_MODELS", (spec,)),
                        patch.object(registry, "_runtime_metadata", return_value=runtime),
                    ):
                        catalog = build_builtin_model_catalog(
                            repo_root,
                            load_observer=observer,
                        )
                    model = catalog["models"][0]
                    self.assertEqual("unavailable", model["availability"]["status"])
                    self.assertIn(
                        "runtime_contract_mismatch",
                        model["availability"]["reason_codes"],
                    )
                    self.assertFalse(model["selectable_for_probe"])
                    self.assertTrue(all(not profile["selectable_for_probe"] for profile in catalog["profiles"]))
                    observer.assert_not_called()

    def test_builtin_weight_size_is_rejected_before_hashing(self) -> None:
        import football_tracking.detector_model_registry as registry

        with tempfile.TemporaryDirectory() as temporary:
            repo_root = Path(temporary)
            weights_root = repo_root / "weights"
            weights_root.mkdir()
            path = weights_root / "fixture.pt"
            path.write_bytes(b"12345678")
            spec = {
                **registry._BUILTIN_MODELS[1],
                "weight_name": path.name,
                "weight_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "weight_size": 8,
            }
            for actual_size in (9, 8 * 1024 * 1024):
                with path.open("wb") as handle:
                    handle.truncate(actual_size)
                with (
                    self.subTest(actual_size=actual_size),
                    patch.object(registry, "_BUILTIN_MODELS", (spec,)),
                    patch.object(registry, "hash_regular_file") as hash_file,
                ):
                    catalog = build_builtin_model_catalog(repo_root)
                hash_file.assert_not_called()
                self.assertEqual("unavailable", catalog["models"][0]["availability"]["status"])
                self.assertIn(
                    "weights_digest_or_size_mismatch",
                    catalog["models"][0]["availability"]["reason_codes"],
                )

    def test_runtime_observation_loads_a_private_snapshot_and_rejects_source_swap(self) -> None:
        import football_tracking.detector_model_registry as registry

        with tempfile.TemporaryDirectory() as temporary:
            repo_root = Path(temporary)
            weights_root = repo_root / "weights"
            weights_root.mkdir()
            trusted = b"trusted"
            changed = b"changed"
            source = weights_root / "fixture.pt"
            source.write_bytes(trusted)
            spec = {
                "model_id": "fixture-model",
                "weight_name": source.name,
                "weight_sha256": hashlib.sha256(trusted).hexdigest(),
                "weight_size": len(trusted),
            }

            def run(command, **_kwargs):
                snapshot = Path(command[-1])
                self.assertNotEqual(source, snapshot)
                self.assertEqual(trusted, snapshot.read_bytes())
                source.write_bytes(changed)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=(
                        'DETECTOR_RUNTIME_OBSERVATION={"direct_load_passed":true,'
                        '"sahi_load_passed":true,"class_names":["sports ball"]}\n'
                    ),
                    stderr="",
                )

            with (
                patch.object(registry, "_BUILTIN_MODELS", (spec,)),
                patch.object(registry.subprocess, "run", side_effect=run),
                self.assertRaisesRegex(DetectorDevelopmentError, "changed"),
            ):
                registry.observe_pinned_model_runtime(repo_root, "fixture-model")

            observation_root = repo_root / "data" / "ball_detector_development_v1" / "model_observations"
            self.assertEqual([], list(observation_root.glob("*.json")))
            self.assertEqual([], list(observation_root.glob(".*.runtime-snapshot-*")))


class DetectorImportTests(unittest.TestCase):
    def setUp(self) -> None:
        import football_tracking.detector_model_import as model_import

        with model_import._IMPORT_VERIFICATION_CACHE_LOCK:
            model_import._IMPORT_VERIFICATION_CACHE.clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temporary.name)
        self.inbox = self.repo_root / "data" / "ball_detector_development_v1" / "import_inbox"
        self.service = DetectorDevelopmentService(self.repo_root, auto_start_workers=False)

    def tearDown(self) -> None:
        import football_tracking.detector_model_import as model_import

        self.service.close()
        self.temporary.cleanup()
        with model_import._IMPORT_VERIFICATION_CACHE_LOCK:
            model_import._IMPORT_VERIFICATION_CACHE.clear()

    def _package(self, name: str = "camera-model", *, lifecycle_state: str = "unverified") -> dict[str, object]:
        package = self.inbox / name
        package.mkdir(parents=True)
        weights = package / "weights.pt"
        weights.write_bytes(b"fixture detector weights")
        descriptor = {
            "schema_version": "1.0",
            "artifact_type": "detector_model_import_package",
            "model_id": "local-camera-yolo11s",
            "version": "camera-a-1",
            "display_name": "Local camera YOLO11s",
            "architecture_family": "yolo11",
            "source": {
                "project": "local-camera-adaptation",
                "version": "camera-a-1",
                "acquisition_method": "server_lineage_package",
            },
            "weights": {"relative_path": "weights.pt", "sha256": _sha256(weights)},
            "class_names": ["ball"],
            "class_map": {"ball": "ball"},
            "input": {"image_size": 1280, "precision": "fp32", "device": "cpu"},
            "memory_envelope": {"max_ram_mb": 4096, "max_vram_mb": 0},
            "licenses": {
                kind: {
                    "name": f"fixture-{kind}",
                    "spdx_id": "MIT",
                    "url": f"https://example.invalid/{kind}",
                    "reviewed": True,
                    "approved_for_local_probe": True,
                }
                for kind in ("dataset", "model", "runtime", "deployment")
            },
            "egress": {
                "frames_leave_local_machine": False,
                "destination": None,
                "operator_consent": "not_required",
            },
            "lifecycle_state": lifecycle_state,
            "bindings": {
                "source_sha256": SHA_A,
                "temporal_group_sha256": SHA_B,
                "camera_profile_sha256": SHA_C,
                "evaluation_package_sha256": None,
                "threshold_profile_sha256": SHA_A,
                "code_commit": "fixture",
                "environment_sha256": SHA_B,
            },
        }
        manifest = package / "descriptor.json"
        _atomic_json(manifest, descriptor)
        return {
            "package_relative_path": name,
            "manifest_sha256": _sha256(manifest),
        }

    def test_import_copies_verified_content_and_is_content_idempotent(self) -> None:
        request = self._package()

        first = self.service.import_model(request)
        second = self.service.import_model(request)

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(
            first["model"]["descriptor"]["descriptor_sha256"], second["model"]["descriptor"]["descriptor_sha256"]
        )
        copied = self.repo_root / first["model"]["descriptor"]["weights"]["relative_path"]
        self.assertEqual(b"fixture detector weights", copied.read_bytes())
        self.assertEqual("blocked", first["model"]["availability"]["status"])
        self.assertIn("server_validation_required", first["model"]["availability"]["reason_codes"])

    def test_import_rejects_builtin_exact_identity_but_allows_a_new_version(self) -> None:
        exact = self._package("builtin-exact")
        exact_manifest = self.inbox / "builtin-exact" / "descriptor.json"
        exact_payload = json.loads(exact_manifest.read_text(encoding="utf-8"))
        exact_payload["model_id"] = "official-coco-yolo11n"
        exact_payload["version"] = "yolo11n-coco-v8.4.0"
        _atomic_json(exact_manifest, exact_payload)

        with self.assertRaisesRegex(DetectorDevelopmentError, "conflict"):
            self.service.import_model({**exact, "manifest_sha256": _sha256(exact_manifest)})

        distinct = self._package("builtin-distinct")
        distinct_manifest = self.inbox / "builtin-distinct" / "descriptor.json"
        distinct_payload = json.loads(distinct_manifest.read_text(encoding="utf-8"))
        distinct_payload["model_id"] = "official-coco-yolo11n"
        distinct_payload["version"] = "camera-a-v2"
        _atomic_json(distinct_manifest, distinct_payload)
        imported = self.service.import_model({**distinct, "manifest_sha256": _sha256(distinct_manifest)})

        self.assertTrue(imported["created"])
        versions = {
            item["descriptor"]["version"]
            for item in self.service.list_models()["models"]
            if item["descriptor"]["model_id"] == "official-coco-yolo11n"
        }
        self.assertEqual({"yolo11n-coco-v8.4.0", "camera-a-v2"}, versions)

    def test_catalog_read_rejects_duplicate_exact_composite_identity(self) -> None:
        import football_tracking.detector_development as development

        builtin = build_builtin_model_catalog(self.repo_root)["models"][0]
        with patch.object(
            development,
            "load_imported_model_records",
            return_value=[deepcopy(builtin)],
        ):
            with self.assertRaisesRegex(DetectorDevelopmentError, "identity"):
                self.service.list_models()

    def test_import_rejects_absolute_traversal_link_directory_digest_and_unsafe_lifecycle(self) -> None:
        request = self._package()
        invalid_requests = [
            {**request, "package_relative_path": str((self.inbox / "camera-model").resolve())},
            {**request, "package_relative_path": "../camera-model"},
            {**request, "manifest_sha256": SHA_A},
        ]
        for invalid in invalid_requests:
            with self.subTest(invalid=invalid), self.assertRaises(DetectorDevelopmentError):
                self.service.import_model(invalid)

        directory_package = self.inbox / "directory-package"
        directory_package.mkdir()
        (directory_package / "descriptor.json").mkdir()
        with self.assertRaises(DetectorDevelopmentError):
            self.service.import_model({"package_relative_path": "directory-package", "manifest_sha256": SHA_A})

        unsafe = self._package("qualified", lifecycle_state="source_segment_qualified")
        with self.assertRaises(DetectorDevelopmentError):
            self.service.import_model(unsafe)

        link_package = self.inbox / "link-package"
        link_package.mkdir()
        try:
            (link_package / "descriptor.json").symlink_to(self.inbox / "camera-model" / "descriptor.json")
        except OSError:
            pass
        else:
            with self.assertRaises(DetectorDevelopmentError):
                self.service.import_model(
                    {
                        "package_relative_path": "link-package",
                        "manifest_sha256": request["manifest_sha256"],
                    }
                )

    def test_import_rejects_incomplete_license_external_egress_and_conflicting_identity(self) -> None:
        request = self._package()
        manifest = self.inbox / "camera-model" / "descriptor.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        del payload["licenses"]["deployment"]
        _atomic_json(manifest, payload)
        with self.assertRaisesRegex(DetectorDevelopmentError, "license"):
            self.service.import_model({**request, "manifest_sha256": _sha256(manifest)})

        payload["licenses"]["deployment"] = {
            "name": "fixture-deployment",
            "spdx_id": "MIT",
            "url": "https://example.invalid",
            "reviewed": True,
            "approved_for_local_probe": True,
        }
        payload["egress"]["frames_leave_local_machine"] = True
        payload["egress"]["destination"] = "https://example.invalid/inference"
        payload["egress"]["operator_consent"] = "approved"
        _atomic_json(manifest, payload)
        with self.assertRaisesRegex(DetectorDevelopmentError, "egress"):
            self.service.import_model({**request, "manifest_sha256": _sha256(manifest)})

        first_request = self._package("first")
        self.service.import_model(first_request)
        conflicting = self._package("second")
        weights = self.inbox / "second" / "weights.pt"
        weights.write_bytes(b"different weights")
        second_manifest = self.inbox / "second" / "descriptor.json"
        second_payload = json.loads(second_manifest.read_text(encoding="utf-8"))
        second_payload["weights"]["sha256"] = _sha256(weights)
        _atomic_json(second_manifest, second_payload)
        with self.assertRaisesRegex(DetectorDevelopmentError, "conflict"):
            self.service.import_model({**conflicting, "manifest_sha256": _sha256(second_manifest)})

    def test_import_requires_explicit_license_approval_and_rejects_existing_descriptor_tamper(self) -> None:
        request = self._package()
        manifest = self.inbox / "camera-model" / "descriptor.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        del payload["licenses"]["model"]["approved_for_local_probe"]
        _atomic_json(manifest, payload)
        with self.assertRaisesRegex(DetectorDevelopmentError, "license"):
            self.service.import_model({**request, "manifest_sha256": _sha256(manifest)})

        denied_request = self._package("denied")
        denied_manifest = self.inbox / "denied" / "descriptor.json"
        denied_payload = json.loads(denied_manifest.read_text(encoding="utf-8"))
        denied_payload["licenses"]["deployment"]["approved_for_local_probe"] = False
        _atomic_json(denied_manifest, denied_payload)
        with self.assertRaisesRegex(DetectorDevelopmentError, "approved"):
            self.service.import_model({**denied_request, "manifest_sha256": _sha256(denied_manifest)})

        request = self._package("approved")
        first = self.service.import_model(request)
        descriptor_path = (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "models"
            / first["model"]["descriptor"]["model_id"]
            / first["model"]["descriptor"]["version"]
            / "descriptor.json"
        )
        tampered = json.loads(descriptor_path.read_text(encoding="utf-8"))
        tampered["display_name"] = "tampered derived descriptor"
        tampered["descriptor_sha256"] = canonical_sha256(
            {key: value for key, value in tampered.items() if key != "descriptor_sha256"}
        )
        _atomic_json(descriptor_path, tampered)

        with self.assertRaisesRegex(DetectorDevelopmentError, "conflict"):
            self.service.import_model(request)

    def test_import_fails_closed_when_file_identity_changes_during_copy(self) -> None:
        request = self._package()

        with patch("football_tracking.detector_model_import._snapshot_identity_is_current", return_value=False):
            with self.assertRaisesRegex(DetectorDevelopmentError, "changed"):
                self.service.import_model(request)

        registry_root = self.repo_root / "data" / "ball_detector_development_v1" / "models"
        self.assertFalse(any(registry_root.rglob("descriptor.json")))

    def test_import_removes_only_its_verified_publish_on_post_publish_failure(self) -> None:
        request = self._package()

        with patch(
            "football_tracking.detector_model_import._verify_published_import",
            side_effect=DetectorDevelopmentError("fixture_failure", "post-publish verification failed"),
        ):
            with self.assertRaisesRegex(DetectorDevelopmentError, "verification failed"):
                self.service.import_model(request)

        registry_root = self.repo_root / "data" / "ball_detector_development_v1" / "models"
        self.assertFalse(any(registry_root.rglob("descriptor.json")))

    def test_import_rejects_a_link_at_the_final_model_identity_without_touching_its_target(self) -> None:
        request = self._package()
        target = self.repo_root / "outside-target"
        target.mkdir()
        marker = target / "do-not-touch.txt"
        marker.write_text("preserve", encoding="utf-8")
        final_dir = (
            self.repo_root / "data" / "ball_detector_development_v1" / "models" / "local-camera-yolo11s" / "camera-a-1"
        )
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            final_dir.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlink creation is unavailable on this host")

        with self.assertRaises(DetectorDevelopmentError):
            self.service.import_model(request)

        self.assertEqual("preserve", marker.read_text(encoding="utf-8"))

    def test_import_rejects_cross_platform_unsafe_storage_segments_and_surrogates(self) -> None:
        cases = [
            ("con", "camera-a-1"),
            ("local-camera-yolo11s", "aux"),
            ("local-camera-yolo11s", "lpt1"),
            ("local-camera-yolo11s", "foo."),
        ]
        for index, (model_id, version) in enumerate(cases):
            request = self._package(f"unsafe-{index}")
            manifest = self.inbox / f"unsafe-{index}" / "descriptor.json"
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["model_id"] = model_id
            payload["version"] = version
            _atomic_json(manifest, payload)
            with self.subTest(model_id=model_id, version=version), self.assertRaises(DetectorDevelopmentError):
                self.service.import_model({**request, "manifest_sha256": _sha256(manifest)})

        request = self._package("surrogate")
        manifest = self.inbox / "surrogate" / "descriptor.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["display_name"] = "bad-\ud800"
        _atomic_json(manifest, payload)
        with self.assertRaises(DetectorDevelopmentError) as raised:
            self.service.import_model({**request, "manifest_sha256": _sha256(manifest)})
        self.assertEqual(400, raised.exception.status_code)

    def test_catalog_revalidation_rejects_weight_and_manifest_tamper(self) -> None:
        request = self._package()
        imported = self.service.import_model(request)
        descriptor = imported["model"]["descriptor"]
        final_dir = (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "models"
            / descriptor["model_id"]
            / descriptor["version"]
        )
        weights = self.repo_root / descriptor["weights"]["relative_path"]
        original = weights.read_bytes()
        weights.write_bytes(b"x" * len(original))
        self.assertNotIn(
            descriptor["model_id"],
            {item["descriptor"]["model_id"] for item in self.service.list_models()["models"]},
        )

        weights.write_bytes(original)
        artifact_manifest = final_dir / "artifact-manifest.json"
        artifact_payload = json.loads(artifact_manifest.read_text(encoding="utf-8"))
        artifact_payload["unexpected"] = True
        _atomic_json(artifact_manifest, artifact_payload)
        self.assertNotIn(
            descriptor["model_id"],
            {item["descriptor"]["model_id"] for item in self.service.list_models()["models"]},
        )

    def test_catalog_verification_cache_hashes_weights_once_across_repeated_lists(
        self,
    ) -> None:
        import football_tracking.detector_model_import as model_import

        imported = self.service.import_model(self._package())
        weights = self.repo_root / imported["model"]["descriptor"]["weights"]["relative_path"]
        with model_import._IMPORT_VERIFICATION_CACHE_LOCK:
            model_import._IMPORT_VERIFICATION_CACHE.clear()
        original = model_import.hash_regular_file
        weight_hashes = 0

        def counted(path, *args, **kwargs):
            nonlocal weight_hashes
            if Path(path) == weights:
                weight_hashes += 1
            return original(path, *args, **kwargs)

        with patch.object(model_import, "hash_regular_file", side_effect=counted):
            first = model_import.load_imported_model_records(self.repo_root)
            second = model_import.load_imported_model_records(self.repo_root)

        self.assertEqual(1, weight_hashes)
        self.assertEqual(first, second)
        self.assertEqual(1, len(first))

    def test_catalog_cache_invalidates_restored_mtime_files_and_ancestor_replacement(
        self,
    ) -> None:
        import football_tracking.detector_model_import as model_import

        imported = self.service.import_model(self._package())
        descriptor = imported["model"]["descriptor"]
        final_dir = (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "models"
            / descriptor["model_id"]
            / descriptor["version"]
        )
        weights = self.repo_root / descriptor["weights"]["relative_path"]
        with model_import._IMPORT_VERIFICATION_CACHE_LOCK:
            model_import._IMPORT_VERIFICATION_CACHE.clear()
        original_hash = model_import.hash_regular_file
        weight_hashes = 0

        def counted(path, *args, **kwargs):
            nonlocal weight_hashes
            if Path(path) == weights:
                weight_hashes += 1
            return original_hash(path, *args, **kwargs)

        with patch.object(model_import, "hash_regular_file", side_effect=counted):
            self.assertEqual(1, len(model_import.load_imported_model_records(self.repo_root)))
            self.assertEqual(1, weight_hashes)

            for name in ("descriptor.json", "artifact-manifest.json"):
                path = final_dir / name
                content = path.read_bytes()
                metadata = path.stat()
                path.write_bytes(content)
                os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
                self.assertEqual(1, len(model_import.load_imported_model_records(self.repo_root)))
            original_weights = weights.read_bytes()
            weight_metadata = weights.stat()
            weights.write_bytes(original_weights)
            os.utime(
                weights,
                ns=(weight_metadata.st_atime_ns, weight_metadata.st_mtime_ns),
            )
            self.assertEqual(1, len(model_import.load_imported_model_records(self.repo_root)))

            model_parent = final_dir.parent
            replaced_parent = model_parent.with_name(f"{model_parent.name}-old")
            model_parent.rename(replaced_parent)
            shutil.copytree(replaced_parent, model_parent)
            self.assertEqual(1, len(model_import.load_imported_model_records(self.repo_root)))

        self.assertEqual(5, weight_hashes)

    def test_catalog_cache_concurrent_miss_hashes_large_weight_once(self) -> None:
        import football_tracking.detector_model_import as model_import

        imported = self.service.import_model(self._package())
        weights = self.repo_root / imported["model"]["descriptor"]["weights"]["relative_path"]
        with model_import._IMPORT_VERIFICATION_CACHE_LOCK:
            model_import._IMPORT_VERIFICATION_CACHE.clear()
        original = model_import.hash_regular_file
        counter_lock = threading.Lock()
        weight_hashes = 0
        results: list[list[dict[str, object]]] = []

        def counted(path, *args, **kwargs):
            nonlocal weight_hashes
            if Path(path) == weights:
                with counter_lock:
                    weight_hashes += 1
                time.sleep(0.05)
            return original(path, *args, **kwargs)

        def load() -> None:
            results.append(model_import.load_imported_model_records(self.repo_root))

        with patch.object(model_import, "hash_regular_file", side_effect=counted):
            workers = [threading.Thread(target=load) for _ in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(5)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(1, weight_hashes)
        self.assertEqual(8, len(results))
        self.assertTrue(all(len(result) == 1 for result in results))

    def test_catalog_cache_sibling_import_does_not_rehash_existing_model(self) -> None:
        import football_tracking.detector_model_import as model_import

        first = self.service.import_model(self._package())
        first_weights = self.repo_root / first["model"]["descriptor"]["weights"]["relative_path"]
        second_request = self._package("camera-model-v2")
        second_manifest = self.inbox / "camera-model-v2" / "descriptor.json"
        second_payload = json.loads(second_manifest.read_text(encoding="utf-8"))
        second_payload["version"] = "camera-a-2"
        _atomic_json(second_manifest, second_payload)
        second_request["manifest_sha256"] = _sha256(second_manifest)
        second_weights = first_weights.parent.parent / "camera-a-2" / "weights.pt"
        with model_import._IMPORT_VERIFICATION_CACHE_LOCK:
            model_import._IMPORT_VERIFICATION_CACHE.clear()
        original = model_import.hash_regular_file
        published_hashes = {first_weights: 0, second_weights: 0}

        def counted(path, *args, **kwargs):
            candidate = Path(path)
            if candidate in published_hashes:
                published_hashes[candidate] += 1
            return original(path, *args, **kwargs)

        with patch.object(model_import, "hash_regular_file", side_effect=counted):
            first_records = model_import.load_imported_model_records(self.repo_root)
            self.assertEqual(1, len(first_records))
            second = self.service.import_model(second_request)
            both_records = model_import.load_imported_model_records(self.repo_root)
            second_final = (
                self.repo_root
                / "data"
                / "ball_detector_development_v1"
                / "models"
                / second["model"]["descriptor"]["model_id"]
                / second["model"]["descriptor"]["version"]
            )
            shutil.rmtree(second_final)
            remaining_records = model_import.load_imported_model_records(self.repo_root)

        self.assertEqual(2, len(both_records))
        self.assertEqual(1, len(remaining_records))
        self.assertEqual(1, published_hashes[first_weights])
        self.assertEqual(1, published_hashes[second_weights])

    def test_catalog_cache_returns_private_copy_and_invalidates_listing_changes(
        self,
    ) -> None:
        import football_tracking.detector_model_import as model_import

        imported = self.service.import_model(self._package())
        descriptor = imported["model"]["descriptor"]
        final_dir = (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "models"
            / descriptor["model_id"]
            / descriptor["version"]
        )
        first = model_import.load_imported_model_records(self.repo_root)
        first[0]["descriptor"]["display_name"] = "caller mutation"
        second = model_import.load_imported_model_records(self.repo_root)
        self.assertEqual("Local camera YOLO11s", second[0]["descriptor"]["display_name"])

        extra = final_dir / "unexpected.bin"
        extra.write_bytes(b"unexpected")
        self.assertEqual([], model_import.load_imported_model_records(self.repo_root))
        extra.unlink()
        self.assertEqual(1, len(model_import.load_imported_model_records(self.repo_root)))

        source_descriptor = final_dir / "source-descriptor.json"
        source_bytes = source_descriptor.read_bytes()
        source_descriptor.unlink()
        self.assertEqual([], model_import.load_imported_model_records(self.repo_root))
        source_descriptor.write_bytes(source_bytes)
        self.assertEqual(1, len(model_import.load_imported_model_records(self.repo_root)))

    def test_catalog_revalidation_rejects_self_consistent_unsafe_source_weight_paths(self) -> None:
        request = self._package()
        imported = self.service.import_model(request)
        descriptor = imported["model"]["descriptor"]
        final_dir = (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "models"
            / descriptor["model_id"]
            / descriptor["version"]
        )
        source_path = final_dir / "source-descriptor.json"
        descriptor_path = final_dir / "descriptor.json"
        manifest_path = final_dir / "artifact-manifest.json"
        source_payload = json.loads(source_path.read_text(encoding="utf-8"))
        source_payload["weights"]["relative_path"] = "../../outside.pt"
        _atomic_json(source_path, source_payload)

        descriptor_payload = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor_payload["import_manifest_sha256"] = _sha256(source_path)
        descriptor_payload["descriptor_sha256"] = canonical_sha256(
            {key: value for key, value in descriptor_payload.items() if key != "descriptor_sha256"}
        )
        _atomic_json(descriptor_path, descriptor_payload)

        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_payload["descriptor_sha256"] = descriptor_payload["descriptor_sha256"]
        source_entry = next(item for item in manifest_payload["artifacts"] if item["name"] == "source_descriptor")
        source_entry["sha256"] = _sha256(source_path)
        source_entry["size_bytes"] = source_path.stat().st_size
        _atomic_json(manifest_path, manifest_payload)

        self.assertNotIn(
            descriptor["model_id"],
            {item["descriptor"]["model_id"] for item in self.service.list_models()["models"]},
        )


class ProbeContractTests(unittest.TestCase):
    @staticmethod
    def _git(root: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return completed.stdout.strip()

    @staticmethod
    def _git_bytes(root: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            input=input_bytes,
            stdin=subprocess.DEVNULL if input_bytes is None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return completed.stdout

    def _commit_binding_fixture(
        self,
        *,
        include_second: bool = True,
        crlf_worktree: bool = False,
        constant_clean_filter: bool = False,
    ) -> tuple[tempfile.TemporaryDirectory, Path, tuple[str, ...], str]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        paths = (
            "python_backend/football_tracking/detector_probe.py",
            "python_backend/football_tracking/api/schemas.py",
        )
        self._git(root, "init")
        self._git(root, "config", "user.email", "probe-tests@example.invalid")
        self._git(root, "config", "user.name", "Probe Tests")
        self._git(root, "config", "core.autocrlf", "true" if crlf_worktree else "false")
        if constant_clean_filter:
            self._git(
                root,
                "config",
                "filter.constant.clean",
                "printf 'trusted-blob\\n'",
            )
            (root / ".gitattributes").write_text(
                "".join(
                    f"{path} filter=constant\n"
                    for path in paths[: 2 if include_second else 1]
                ),
                encoding="utf-8",
            )
        for path in paths[: 2 if include_second else 1]:
            target = root / Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            newline = b"\r\n" if crlf_worktree else b"\n"
            target.write_bytes(b"# " + path.encode("utf-8") + newline)
        files_to_add = list(paths[: 2 if include_second else 1])
        if constant_clean_filter:
            files_to_add.append(".gitattributes")
        self._git(root, "add", "--", *files_to_add)
        self._git(root, "commit", "-m", "fixture")
        return temporary, root, paths, self._git(root, "rev-parse", "--verify", "HEAD").lower()

    @staticmethod
    def _assert_unbound_commit_evidence(binding) -> None:
        assert binding.code_commit is None
        assert binding.code_commit_status == "unbound"
        assert binding.code_commit_reason == "code_bundle_differs_from_commit"
        assert binding.code_commit_blob_files is None
        assert binding.code_commit_blob_bundle_sha256 is None
        assert binding.code_commit_binding_kind is None

    @staticmethod
    def _assert_unavailable_commit_evidence(binding) -> None:
        assert binding.code_commit is None
        assert binding.code_commit_status == "unavailable"
        assert binding.code_commit_reason == "repository_commit_unavailable"
        assert binding.code_commit_blob_files is None
        assert binding.code_commit_blob_bundle_sha256 is None
        assert binding.code_commit_binding_kind is None

    def test_code_commit_binding_distinguishes_crlf_worktree_from_lf_blobs(self) -> None:
        from football_tracking.detector_probe import DetectorProbeCoordinator

        temporary, root, paths, commit = self._commit_binding_fixture(crlf_worktree=True)
        try:
            raw_worktree = {path: (root / Path(path)).read_bytes() for path in paths}
            raw_blobs = {
                path: self._git_bytes(root, "cat-file", "blob", f"{commit}:{path}")
                for path in paths
            }
            self.assertTrue(all(b"\r\n" in content for content in raw_worktree.values()))
            self.assertTrue(all(b"\r\n" not in content for content in raw_blobs.values()))

            binding = DetectorProbeCoordinator._code_commit_binding(root, paths)

            self.assertEqual(commit, binding.code_commit)
            self.assertEqual("bound", binding.code_commit_status)
            self.assertIsNone(binding.code_commit_reason)
            self.assertEqual(
                "exact_or_crlf_to_lf_commit_blob",
                binding.code_commit_binding_kind,
            )
            self.assertEqual(
                {path: hashlib.sha256(content).hexdigest() for path, content in raw_worktree.items()},
                binding.worktree_files,
            )
            self.assertEqual(
                {path: hashlib.sha256(content).hexdigest() for path, content in raw_blobs.items()},
                binding.code_commit_blob_files,
            )
            self.assertNotEqual(binding.worktree_files, binding.code_commit_blob_files)
            self.assertEqual(
                canonical_sha256(binding.code_commit_blob_files),
                binding.code_commit_blob_bundle_sha256,
            )
            self.assertEqual(raw_worktree, {path: (root / Path(path)).read_bytes() for path in paths})
        finally:
            temporary.cleanup()

    def test_code_commit_binding_requires_every_allowlisted_path_to_match_head(self) -> None:
        from football_tracking.detector_probe import DetectorProbeCoordinator

        temporary, root, paths, commit = self._commit_binding_fixture()
        try:
            binding = DetectorProbeCoordinator._code_commit_binding(root, paths)
            self.assertEqual(commit, binding.code_commit)
            self.assertEqual("bound", binding.code_commit_status)
            self.assertIsNone(binding.code_commit_reason)
            self.assertEqual(
                "exact_or_crlf_to_lf_commit_blob",
                binding.code_commit_binding_kind,
            )
            self.assertEqual(
                canonical_sha256(binding.code_commit_blob_files),
                binding.code_commit_blob_bundle_sha256,
            )
            unrelated = root / "unrelated.txt"
            unrelated.write_text("unrelated dirty state\n", encoding="utf-8")
            binding = DetectorProbeCoordinator._code_commit_binding(root, paths)
            self.assertEqual(commit, binding.code_commit)
            self.assertEqual("bound", binding.code_commit_status)

            tracked = root / Path(paths[0])
            tracked.write_text("modified\n", encoding="utf-8")
            self._assert_unbound_commit_evidence(
                DetectorProbeCoordinator._code_commit_binding(root, paths)
            )
            self._git(root, "restore", "--", paths[0])
            tracked.unlink()
            self._assert_unbound_commit_evidence(
                DetectorProbeCoordinator._code_commit_binding(root, paths)
            )
            self._git(root, "restore", "--", paths[0])
            renamed = "python_backend/football_tracking/renamed_probe.py"
            self._git(root, "mv", paths[0], renamed)
            self._assert_unbound_commit_evidence(
                DetectorProbeCoordinator._code_commit_binding(root, paths)
            )
        finally:
            temporary.cleanup()

        temporary, root, paths, _commit = self._commit_binding_fixture(include_second=False)
        try:
            untracked = root / Path(paths[1])
            untracked.parent.mkdir(parents=True, exist_ok=True)
            untracked.write_text("untracked\n", encoding="utf-8")
            self._assert_unbound_commit_evidence(
                DetectorProbeCoordinator._code_commit_binding(root, paths)
            )
        finally:
            temporary.cleanup()

    def test_code_commit_binding_reports_git_unavailable(self) -> None:
        from football_tracking.detector_probe import DetectorProbeCoordinator

        failures = (
            OSError("git unavailable"),
            subprocess.TimeoutExpired(["git"], 2),
            subprocess.CompletedProcess(["git"], 1, stdout=b"", stderr=b"failed"),
            subprocess.CompletedProcess(["git"], 0, stdout=b"not-a-commit\n", stderr=b""),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch(
                    "football_tracking.detector_probe.subprocess.run",
                    side_effect=failure if isinstance(failure, BaseException) else None,
                    return_value=None if isinstance(failure, BaseException) else failure,
                ):
                    self._assert_unavailable_commit_evidence(
                        DetectorProbeCoordinator._code_commit_binding(
                            Path("unused"),
                            ("python_backend/football_tracking/detector_probe.py",),
                        )
                    )

        for unsafe_path in (
            ":(glob)python_backend/**/*.py",
            "../detector_probe.py",
            "/python_backend/detector_probe.py",
            "python_backend\\detector_probe.py",
            "python_backend/detector_probe.py\0suffix",
        ):
            with self.subTest(unsafe_path=unsafe_path):
                self._assert_unavailable_commit_evidence(
                    DetectorProbeCoordinator._code_commit_binding(
                        Path("unused"),
                        (unsafe_path,),
                    )
                )

    def test_code_commit_binding_ignores_inherited_git_redirection_environment(self) -> None:
        from football_tracking.detector_probe import DetectorProbeCoordinator

        first_temporary, first_root, paths, first_commit = (
            self._commit_binding_fixture()
        )
        second_temporary, second_root, second_paths, _second_commit = (
            self._commit_binding_fixture()
        )
        original_run = subprocess.run
        observed_environments: list[dict[str, str]] = []
        try:
            redirected = second_root / Path(second_paths[0])
            redirected.write_text("# redirected repository\n", encoding="utf-8")
            self._git(second_root, "add", "--", second_paths[0])
            self._git(second_root, "commit", "-m", "redirected fixture")
            second_commit = self._git(
                second_root,
                "rev-parse",
                "--verify",
                "HEAD",
            ).lower()
            self.assertNotEqual(first_commit, second_commit)

            def inspect_environment(command, **kwargs):
                observed_environments.append(dict(kwargs["env"]))
                return original_run(command, **kwargs)

            malicious_git_environment = {
                "GIT_DIR": str(second_root / ".git"),
                "GIT_WORK_TREE": str(second_root),
                "GIT_CONFIG_COUNT": "not-an-integer",
                "GIT_CONFIG_KEY_0": "core.bare",
                "GIT_CONFIG_VALUE_0": "true",
            }
            with (
                patch.dict(os.environ, malicious_git_environment, clear=False),
                patch(
                    "football_tracking.detector_probe.subprocess.run",
                    side_effect=inspect_environment,
                ),
            ):
                binding = DetectorProbeCoordinator._code_commit_binding(
                    first_root,
                    paths,
                )

            self.assertEqual(first_commit, binding.code_commit)
            self.assertEqual("bound", binding.code_commit_status)
            self.assertTrue(observed_environments)
            for environment in observed_environments:
                inherited_git_names = {
                    name
                    for name in environment
                    if name.upper().startswith("GIT_")
                }
                self.assertEqual({"GIT_TERMINAL_PROMPT"}, inherited_git_names)
                self.assertEqual("0", environment["GIT_TERMINAL_PROMPT"])
        finally:
            second_temporary.cleanup()
            first_temporary.cleanup()

    def test_code_commit_binding_rejects_mismatched_reported_repository_root(self) -> None:
        from football_tracking.detector_probe import DetectorProbeCoordinator

        first_temporary, first_root, paths, _commit = self._commit_binding_fixture()
        second_temporary, second_root, _second_paths, _second_commit = (
            self._commit_binding_fixture()
        )
        original_run = subprocess.run
        try:
            def redirect_reported_root(command, **kwargs):
                if command[1:] == ["rev-parse", "--show-toplevel"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=str(second_root).encode("utf-8") + b"\n",
                        stderr=b"",
                    )
                return original_run(command, **kwargs)

            with patch(
                "football_tracking.detector_probe.subprocess.run",
                side_effect=redirect_reported_root,
            ):
                self._assert_unavailable_commit_evidence(
                    DetectorProbeCoordinator._code_commit_binding(first_root, paths)
                )
        finally:
            second_temporary.cleanup()
            first_temporary.cleanup()

    def test_code_commit_binding_rejects_arbitrary_clean_filter_equivalence(self) -> None:
        from football_tracking.detector_probe import DetectorProbeCoordinator

        temporary, root, paths, commit = self._commit_binding_fixture(
            constant_clean_filter=True
        )
        try:
            status = self._git_bytes(
                root,
                "--literal-pathspecs",
                "status",
                "--porcelain=v1",
                "-z",
                "--",
                *paths,
            )
            self.assertEqual(b"", status)
            raw_worktree = {path: (root / Path(path)).read_bytes() for path in paths}
            raw_blobs = {
                path: self._git_bytes(root, "cat-file", "blob", f"{commit}:{path}")
                for path in paths
            }
            self.assertTrue(
                all(content == b"trusted-blob\n" for content in raw_blobs.values())
            )
            self.assertTrue(
                all(content != raw_blobs[path] for path, content in raw_worktree.items())
            )

            self._assert_unbound_commit_evidence(
                DetectorProbeCoordinator._code_commit_binding(root, paths)
            )
        finally:
            temporary.cleanup()

    def test_code_commit_binding_fails_closed_on_git_head_and_worktree_drift(self) -> None:
        from football_tracking.detector_probe import DetectorProbeCoordinator

        temporary, root, paths, _commit = self._commit_binding_fixture(
            crlf_worktree=True
        )
        original_run = subprocess.run
        try:
            def git_failure(command, **kwargs):
                if command[1:3] == ["cat-file", "blob"]:
                    raise subprocess.TimeoutExpired(command, 2)
                return original_run(command, **kwargs)

            with patch(
                "football_tracking.detector_probe.subprocess.run",
                side_effect=git_failure,
            ):
                self._assert_unavailable_commit_evidence(
                    DetectorProbeCoordinator._code_commit_binding(root, paths)
                )

            rev_parse_calls = 0

            def drift_head_after_capture(command, **kwargs):
                nonlocal rev_parse_calls
                if command[1:] == ["rev-parse", "--verify", "HEAD"]:
                    rev_parse_calls += 1
                    if rev_parse_calls == 2:
                        return subprocess.CompletedProcess(
                            command,
                            0,
                            stdout=b"f" * 40 + b"\n",
                            stderr=b"",
                        )
                return original_run(command, **kwargs)

            with patch(
                "football_tracking.detector_probe.subprocess.run",
                side_effect=drift_head_after_capture,
            ):
                self._assert_unavailable_commit_evidence(
                    DetectorProbeCoordinator._code_commit_binding(root, paths)
                )
            self.assertEqual(2, rev_parse_calls)

            drifted = False

            def drift_worktree_after_blob_read(command, **kwargs):
                nonlocal drifted
                completed = original_run(command, **kwargs)
                if command[1:3] == ["cat-file", "blob"] and not drifted:
                    drifted = True
                    target = root / Path(paths[0])
                    target.write_bytes(target.read_bytes().replace(b"\r\n", b"\n"))
                return completed

            with patch(
                "football_tracking.detector_probe.subprocess.run",
                side_effect=drift_worktree_after_blob_read,
            ):
                self._assert_unbound_commit_evidence(
                    DetectorProbeCoordinator._code_commit_binding(root, paths)
                )
            self.assertTrue(drifted)
        finally:
            temporary.cleanup()

    def test_windows_parent_monitor_uses_explicit_process_handle_and_fails_closed(self) -> None:
        import football_tracking.detector_probe_worker as worker

        class Kernel:
            def __init__(self, wait_result: int, close_result: int = 1) -> None:
                self.wait_result = wait_result
                self.close_result = close_result
                self.wait_calls = 0
                self.close_calls = 0

            def WaitForSingleObject(self, handle, milliseconds):
                self.wait_calls += 1
                self.assertions = (handle, milliseconds)
                return self.wait_result

            def CloseHandle(self, handle):
                self.close_calls += 1
                return self.close_result

        live_kernel = Kernel(worker._WAIT_TIMEOUT)
        live = worker._WindowsParentMonitor(live_kernel, 41)
        with patch.object(worker.os, "getppid", return_value=999):
            self.assertTrue(live.is_alive())
        self.assertTrue(live.close())
        self.assertEqual((41, 0), live_kernel.assertions)
        self.assertEqual(1, live_kernel.close_calls)
        self.assertFalse(live.is_alive())

        for wait_result in (0, 0xFFFFFFFF, 7):
            with self.subTest(wait_result=wait_result):
                monitor = worker._WindowsParentMonitor(Kernel(wait_result), 42)
                self.assertFalse(monitor.is_alive())
                self.assertTrue(monitor.close())

        close_failure = worker._WindowsParentMonitor(Kernel(worker._WAIT_TIMEOUT, 0), 43)
        self.assertTrue(close_failure.is_alive())
        self.assertFalse(close_failure.close())

    def test_parent_liveness_and_prestart_deadline_keep_platform_boundaries(self) -> None:
        import football_tracking.detector_probe_worker as worker

        class Monitor:
            def __init__(self, *, alive: bool, closed: bool = True) -> None:
                self.alive = alive
                self.closed = closed

            def is_alive(self) -> bool:
                return self.alive

            def close(self) -> bool:
                return self.closed

        with (
            patch.object(worker.os, "name", "nt"),
            patch.object(worker.os, "getppid", return_value=999),
            patch.object(worker._WindowsParentMonitor, "open", return_value=Monitor(alive=True)),
        ):
            self.assertTrue(worker._parent_is_alive(111))
        with (
            patch.object(worker.os, "name", "nt"),
            patch.object(worker._WindowsParentMonitor, "open", return_value=None),
        ):
            self.assertFalse(worker._parent_is_alive(111))
        with (
            patch.object(worker.os, "name", "nt"),
            patch.object(
                worker._WindowsParentMonitor,
                "open",
                return_value=Monitor(alive=True, closed=False),
            ),
        ):
            self.assertFalse(worker._parent_is_alive(111))

        with (
            patch.object(worker.os, "name", "posix"),
            patch.object(worker.os, "getppid", return_value=222),
            patch.object(worker.os, "kill") as kill,
        ):
            self.assertFalse(worker._parent_is_alive(111))
            kill.assert_not_called()

        with tempfile.TemporaryDirectory() as temporary:
            control = Path(temporary)
            with patch.object(worker.time, "monotonic", side_effect=(0.0, 10.0)):
                self.assertFalse(
                    worker._wait_for_start(
                        control,
                        "worker-1",
                        111,
                        lambda: True,
                    )
                )

            _atomic_json(
                control / "start.json",
                {
                    "schema_version": "1.0",
                    "artifact_type": "detector_probe_worker_start",
                    "worker_id": "worker-1",
                    "launcher_pid": 333,
                    "parent_pid": 111,
                },
            )
            self.assertTrue(
                worker._wait_for_start(control, "worker-1", 111, lambda: True)
            )
            self.assertFalse(
                worker._wait_for_start(control, "worker-other", 111, lambda: True)
            )

    def test_worker_opens_parent_monitor_once_and_closes_on_every_return(self) -> None:
        import football_tracking.detector_probe_worker as worker

        class Monitor:
            def __init__(self, close_result: bool = True) -> None:
                self.close_result = close_result
                self.close_calls = 0

            @staticmethod
            def is_alive() -> bool:
                return True

            def close(self) -> bool:
                self.close_calls += 1
                return self.close_result

        for worker_result, close_result, expected in ((0, True, 0), (78, True, 78), (0, False, 78)):
            with self.subTest(worker_result=worker_result, close_result=close_result):
                monitor = Monitor(close_result)
                with (
                    patch.object(worker, "_open_parent_monitor", return_value=monitor) as opened,
                    patch.object(worker, "_run_worker_with_monitor", return_value=worker_result),
                ):
                    self.assertEqual(expected, worker.run_worker(Path("control"), Path("staging"), 111))
                opened.assert_called_once_with(111)
                self.assertEqual(1, monitor.close_calls)
        with patch.object(worker, "_open_parent_monitor", side_effect=OSError("OpenProcess unavailable")):
            self.assertEqual(
                worker._WORKER_EXIT_CONTAINMENT_UNAVAILABLE,
                worker.run_worker(Path("control"), Path("staging"), 111),
            )

    def test_runner_preserves_decode_cancellation_as_terminal_cancelled(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch(
                "football_tracking.detector_probe_runner.decode_verified_frames",
                side_effect=CandidateDatasetCancelled("cancelled while decoding"),
            ),
        ):
            with self.assertRaises(DetectorDevelopmentError) as raised:
                run_detector_probe(
                    {
                        "_source_path": str(Path(temporary) / "source.mp4"),
                        "frame_indices": [3],
                        "_source_width": 64,
                        "_source_height": 32,
                        "_source_frame_count": 10,
                        "_requested_decode_mode": "sequential",
                        "_execution_environment": probe_execution_environment(),
                    },
                    [],
                    Path(temporary),
                    lambda: True,
                    lambda *_args: None,
                )

        self.assertEqual("cancelled", raised.exception.code)

    def test_direct_and_sahi_candidates_share_source_pixel_coordinates(self) -> None:
        direct = normalize_probe_candidates(
            [{"bbox": [101.0, 52.0, 109.0, 60.0], "confidence": 0.8, "class_name": "sports ball"}],
            frame_index=9,
            frame_width=5120,
            frame_height=1440,
            mode="direct",
            class_map={"sports ball": "ball"},
        )
        sahi = normalize_probe_candidates(
            [{"bbox": [1.0, 2.0, 9.0, 10.0], "confidence": 0.8, "class_name": "sports ball"}],
            frame_index=9,
            frame_width=5120,
            frame_height=1440,
            mode="sahi",
            tile_origin=(100.0, 50.0),
            class_map={"sports ball": "ball"},
        )

        self.assertEqual(direct["candidates"][0]["bbox_source_px"], sahi["candidates"][0]["bbox_source_px"])
        self.assertEqual("direct_source_coordinates", direct["candidates"][0]["coordinate_reason"])
        self.assertEqual("sahi_tile_offset_applied", sahi["candidates"][0]["coordinate_reason"])

    def test_merge_is_deterministic_bounded_and_preserves_reasons(self) -> None:
        candidates = [
            {
                "frame_index": 1,
                "bbox_source_px": [10.0, 10.0, 20.0, 20.0],
                "confidence": 0.9,
                "class_name": "ball",
                "source": "sahi",
                "coordinate_reason": "sahi_tile_offset_applied",
            },
            {
                "frame_index": 1,
                "bbox_source_px": [10.5, 10.5, 20.5, 20.5],
                "confidence": 0.8,
                "class_name": "ball",
                "source": "sahi",
                "coordinate_reason": "sahi_tile_offset_applied",
            },
        ] + [
            {
                "frame_index": 1,
                "bbox_source_px": [float(30 + index * 10), 10.0, float(35 + index * 10), 15.0],
                "confidence": 0.7 - index * 0.01,
                "class_name": "ball",
                "source": "sahi",
                "coordinate_reason": "sahi_tile_offset_applied",
            }
            for index in range(8)
        ]

        merged = merge_probe_candidates(candidates, top_k=5, iou_threshold=0.5)
        reversed_result = merge_probe_candidates(list(reversed(candidates)), top_k=5, iou_threshold=0.5)

        self.assertEqual(merged, reversed_result)
        self.assertEqual(5, len(merged["candidates"]))
        self.assertEqual(1, merged["rejection_reasons"]["duplicate_suppressed_iou"])
        self.assertEqual(4, merged["rejection_reasons"]["top_k_limit"])
        self.assertEqual(merged["candidates"][0], merged["display_candidate"])

    def test_invalid_coordinates_and_classes_fail_closed_with_reasons(self) -> None:
        normalized = normalize_probe_candidates(
            [
                {"bbox": [-1, 0, 5, 5], "confidence": 0.8, "class_name": "ball"},
                {"bbox": [1, 1, 2, 2], "confidence": 0.8, "class_name": "person"},
                {"bbox": [1, 1, float("nan"), 2], "confidence": 0.8, "class_name": "ball"},
            ],
            frame_index=1,
            frame_width=100,
            frame_height=100,
            mode="direct",
            class_map={"ball": "ball"},
        )
        self.assertEqual([], normalized["candidates"])
        self.assertEqual(
            {"bbox_outside_source": 1, "class_not_mapped": 1, "non_finite_candidate": 1},
            normalized["rejection_reasons"],
        )


class DetectorProbeJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.repo_root = Path(cls.temporary.name)
        weights = cls.repo_root / "weights"
        weights.mkdir()
        (weights / "fixture.pt").write_bytes(b"fixture")
        config = cls.repo_root / "config"
        config.mkdir()
        cls.base_config = config / "base.yaml"
        cls.base_config.write_bytes(b"fixture: base\n")
        cls.base_config_sha256 = _sha256(cls.base_config)
        cls.effective_config = config / "effective.yaml"
        cls.effective_config.write_bytes(b"fixture: effective\n")
        cls.effective_config_sha256 = _sha256(cls.effective_config)
        data = cls.repo_root / "data"
        data.mkdir(exist_ok=True)
        cls.source = data / "source.mp4"
        cls.source.write_bytes(b"bounded source fixture")
        cls.source_sha256 = _sha256(cls.source)
        output = cls.repo_root / "outputs" / "runs" / "trial-1"
        output.mkdir(parents=True)
        cls.contract = output / "tracking_contract.v2.json"
        _atomic_json(
            cls.contract,
            {
                "schema_version": "2.0",
                "source": {
                    "video_sha256": cls.source_sha256,
                    "width": 5120,
                    "height": 1440,
                    "frame_count": 60,
                },
                "summary": {"status": "ok"},
                "frames": [{"frame_index": index} for index in (0, 10, 20, 30, 40, 50)],
                "candidates": [],
                "classifications": [],
                "decisions": [],
                "validation_errors": [],
            },
        )
        cls.contract_sha256 = _sha256(cls.contract)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def setUp(self) -> None:
        development = self.repo_root / "data" / "ball_detector_development_v1"
        shutil.rmtree(development, ignore_errors=True)

    def _request(self, **patch_values: object) -> dict[str, object]:
        request: dict[str, object] = {
            "parent_trial_id": "trial-1",
            "source_id": "source-fixture",
            "source_relative_path": "data/source.mp4",
            "source_sha256": self.source_sha256,
            "tracking_contract_relative_path": "outputs/runs/trial-1/tracking_contract.v2.json",
            "tracking_contract_sha256": self.contract_sha256,
            "base_config_relative_path": "config/base.yaml",
            "base_config_sha256": self.base_config_sha256,
            "effective_config_relative_path": "config/effective.yaml",
            "effective_config_sha256": self.effective_config_sha256,
            "trial_intent_sha256": SHA_C,
            "tuning_patch_binding": deepcopy(EMPTY_TUNING_BINDING),
            "tuning_patch_sha256": EMPTY_TUNING_SHA256,
            "profile_ids": ["official-coco-yolo11n-direct", "official-coco-yolo11n-sahi"],
            "frame_indices": [12, 3, 12, 6],
            "top_k": 5,
        }
        request.update(patch_values)
        return request

    @staticmethod
    def _successful_runner(request, profiles, staging, should_cancel, progress):
        jpeg = _jpeg_fixture()
        frame_rows = []
        for completed, frame_index in enumerate(request["frame_indices"], start=1):
            if should_cancel():
                raise DetectorDevelopmentError("cancelled", "cancelled")
            frame_path = staging / "frames" / f"{frame_index:09d}.jpg"
            frame_path.parent.mkdir(parents=True, exist_ok=True)
            frame_path.write_bytes(jpeg)
            results = []
            for profile in profiles:
                overlay_path = staging / "overlays" / f"{frame_index:09d}-{profile['profile_id']}.jpg"
                overlay_path.parent.mkdir(parents=True, exist_ok=True)
                overlay_path.write_bytes(jpeg)
                candidate = {
                    "frame_index": frame_index,
                    "bbox_source_px": [10.0, 20.0, 18.0, 28.0],
                    "confidence": 0.8,
                    "class_name": "ball",
                    "checkpoint_class_name": "sports ball",
                    "source": f"yolo_{profile['mode']}",
                    "coordinate_reason": (
                        "direct_source_coordinates" if profile["mode"] == "direct" else "sahi_tile_offset_applied"
                    ),
                    "merge_reason": "retained_top_k",
                }
                results.append(
                    {
                        "profile_id": profile["profile_id"],
                        "profile_sha256": profile["profile_sha256"],
                        "status": "completed",
                        "latency_ms": 1.25,
                        "candidate_count": 1,
                        "top_k": 5,
                        "raw_candidates": [candidate],
                        "display_candidate": candidate,
                        "filter_reasons": {},
                        "failure_code": None,
                        "raw_overlay_relative_path": overlay_path.relative_to(staging).as_posix(),
                    }
                )
            frame_rows.append(
                {
                    "frame_index": frame_index,
                    "source_frame_relative_path": frame_path.relative_to(staging).as_posix(),
                    "requested_decode_mode": request["_requested_decode_mode"],
                    "effective_decode_mode": {
                        "sequential": "sequential",
                        "preroll": "preroll_verified",
                        "direct": "direct_verified",
                    }[request["_requested_decode_mode"]],
                    "decoded_frame_position": frame_index,
                    "media_integrity": {
                        "path": None,
                        "status": "ok",
                        "width": request["_source_width"],
                        "height": request["_source_height"],
                        "mean_luma": 90.0,
                        "std_luma": 20.0,
                        "texture_tile_ratio": 0.5,
                        "dominant_color_ratio": 0.2,
                        "gray": False,
                        "low_information": False,
                        "likely_corrupt": False,
                        "reasons": [],
                    },
                    "profile_results": results,
                }
            )
            progress(completed, len(request["frame_indices"]))
        effective_mode = {
            "sequential": "sequential",
            "preroll": "preroll_verified",
            "direct": "direct_verified",
        }[request["_requested_decode_mode"]]
        return {
            "frames": frame_rows,
            "decode": {
                "width": request["_source_width"],
                "height": request["_source_height"],
                "frame_count": request["_source_frame_count"],
                "fps": 30.0,
                "requested_decode_mode": request["_requested_decode_mode"],
                "effective_decode_mode": effective_mode,
                "verified_frame_indices": request["frame_indices"],
                "position_verification": "opencv_next_frame_index_with_0.25_tolerance",
            },
            "execution": {
                "device": request["_execution_environment"]["device"],
                "precision": request["_execution_environment"]["precision"],
            },
        }

    def _service(self, runner=None, *, auto_start=False) -> DetectorDevelopmentService:
        return DetectorDevelopmentService(
            self.repo_root,
            probe_runner=runner or self._successful_runner,
            auto_start_workers=auto_start,
            catalog_provider=self._catalog,
        )

    def _supervised_service(
        self,
        mode: str,
        *,
        auto_start: bool = False,
        deadline: float = 5.0,
        heartbeat_timeout: float = 2.0,
        descendant_pid_path: Path | None = None,
    ) -> DetectorDevelopmentService:
        helper = Path(__file__).parent / "fixtures" / "detector_probe_test_worker.py"

        def command_factory(control: Path, staging: Path, _parent_pid: int) -> list[str]:
            (staging / "test-worker-fixture.jpg").write_bytes(_jpeg_fixture())
            command = [sys.executable, str(helper), mode, str(control), str(staging)]
            if descendant_pid_path is not None:
                command.append(str(descendant_pid_path))
            return command

        return DetectorDevelopmentService(
            self.repo_root,
            probe_runner=None,
            auto_start_workers=auto_start,
            catalog_provider=self._catalog,
            worker_deadline_seconds=deadline,
            worker_heartbeat_timeout_seconds=heartbeat_timeout,
            worker_command_factory=command_factory,
        )

    @staticmethod
    def _wait_status(
        service: DetectorDevelopmentService,
        job_id: str,
        expected: set[str],
        *,
        timeout: float = 10.0,
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = service.get_probe(job_id)
            if record["status"] in expected:
                return record
            time.sleep(0.025)
        raise AssertionError(f"probe {job_id} did not reach {expected}")

    @classmethod
    def _catalog(cls) -> dict[str, object]:
        workspace_root = Path(__file__).resolve().parents[1]
        built = build_builtin_model_catalog(workspace_root)
        model = deepcopy(
            next(item for item in built["models"] if item["descriptor"]["model_id"] == "official-coco-yolo11n")
        )
        descriptor = model["descriptor"]
        descriptor["weights"] = {
            "relative_path": "weights/fixture.pt",
            "sha256": _sha256(cls.repo_root / "weights" / "fixture.pt"),
            "size_bytes": 7,
        }
        descriptor.pop("descriptor_sha256", None)
        descriptor["descriptor_sha256"] = canonical_sha256(descriptor)
        model["availability"] = {
            "status": "available",
            "reason_codes": [],
            "observations": {
                "runtime_load": {
                    "status": "pass",
                    "reason": "fixture_runtime_evidence",
                    "installed_runtime": {
                        "ultralytics": "8.4.31",
                        "sahi": "0.11.36",
                        "torch": "2.7.1+cpu",
                    },
                    "evidence_sha256": SHA_A,
                }
            },
        }
        model["selectable_for_probe"] = True
        profiles = []
        for profile in built["profiles"]:
            if profile["model_id"] != "official-coco-yolo11n":
                continue
            profile = deepcopy(profile)
            profile["model_descriptor_sha256"] = descriptor["descriptor_sha256"]
            profile_base = {
                key: profile[key]
                for key in (
                    "schema_version",
                    "artifact_type",
                    "profile_id",
                    "version",
                    "model_id",
                    "model_version",
                    "model_descriptor_sha256",
                    "mode",
                    "settings",
                )
            }
            profile["profile_sha256"] = canonical_sha256(profile_base)
            profile["availability"] = {
                "status": "available",
                "reason_codes": [],
                "runtime": {
                    "name": "ultralytics" if profile["mode"] == "direct" else "sahi",
                    "installed_version": "fixture",
                    "load_smoke": True,
                },
            }
            profile["selectable_for_probe"] = True
            profiles.append(profile)
        return {
            "schema_version": "1.0",
            "artifact_type": "ball_detector_development_v1",
            "models": [model],
            "profiles": profiles,
            "catalog_findings": [],
        }

    def test_request_is_frozen_sorted_defaulted_and_returns_202_style_envelope(self) -> None:
        service = self._service()
        try:
            created = service.create_probe(self._request())
            status = service.get_probe(created["job_id"])
        finally:
            service.close()

        self.assertEqual("queued", created["status"])
        self.assertEqual("/api/v1/detector-probes/" + created["job_id"], created["status_url"])
        self.assertEqual(created["status_url"] + "/cancel", created["cancel_url"])
        self.assertEqual([3, 6, 12], status["frozen_request"]["frame_indices"])
        self.assertEqual(5, status["frozen_request"]["top_k"])
        self.assertEqual(2, len(status["frozen_profiles"]))
        self.assertTrue(all(item["profile_sha256"] for item in status["frozen_profiles"]))
        bundle = status["frozen_request"]["execution_bundle"]
        self.assertEqual({"sahi", "torch", "ultralytics"}, set(bundle["installed_runtime"]))
        self.assertEqual(
            canonical_sha256(bundle),
            status["frozen_request"]["execution_bundle_sha256"],
        )
        self.assertEqual(
            canonical_sha256(status["frozen_profiles"]),
            status["frozen_profiles_sha256"],
        )
        self.assertEqual(
            bundle["runtime_environment_sha256"],
            status["frozen_request"]["runtime_environment_sha256"],
        )
        self.assertTrue(
            {
                "football_tracking/__init__.py",
                "football_tracking/api/__init__.py",
                "football_tracking/api/schemas.py",
                "football_tracking/ai_contracts.py",
                "football_tracking/ai_improvement_prompt_contract.py",
                "football_tracking/config.py",
                "football_tracking/detector.py",
                "football_tracking/media_integrity.py",
            }.issubset(bundle["code_bundle_files"])
        )
        import football_tracking.detector_probe as probe_module

        package_root = Path(probe_module.__file__).resolve().parent
        self.assertEqual(
            {
                f"football_tracking/{name}": _sha256(package_root / name)
                for name in probe_module._CODE_BUNDLE_FILES
            },
            bundle["code_bundle_files"],
        )
        self.assertEqual(
            canonical_sha256(bundle["code_bundle_files"]),
            bundle["code_bundle_sha256"],
        )
        self.assertIn("code_commit_blob_files", bundle)
        self.assertIn("code_commit_blob_bundle_sha256", bundle)
        self.assertIn("code_commit_binding_kind", bundle)
        if bundle["code_commit_status"] == "bound":
            self.assertEqual(
                set(bundle["code_bundle_files"]),
                set(bundle["code_commit_blob_files"]),
            )
            self.assertEqual(
                canonical_sha256(bundle["code_commit_blob_files"]),
                bundle["code_commit_blob_bundle_sha256"],
            )
            self.assertEqual(
                "exact_or_crlf_to_lf_commit_blob",
                bundle["code_commit_binding_kind"],
            )
        else:
            self.assertIsNone(bundle["code_commit_blob_files"])
            self.assertIsNone(bundle["code_commit_blob_bundle_sha256"])
            self.assertIsNone(bundle["code_commit_binding_kind"])
        from football_tracking.api.schemas import DetectorProbeExecutionBundleView

        DetectorProbeExecutionBundleView.model_validate(bundle)
        self.assertTrue(bundle["execution_environment"]["pydantic_version"])
        self.assertTrue(bundle["execution_environment"]["pydantic_core_version"])

        service = self._service()
        try:
            defaulted = service.create_probe(
                self._request(
                    frame_indices=None,
                    trial_intent_sha256=SHA_A,
                )
            )
            frozen = service.get_probe(defaulted["job_id"])["frozen_request"]
        finally:
            service.close()
        self.assertEqual([0, 10, 20, 30, 40, 50], frozen["frame_indices"])

    def test_frozen_bundle_rejects_version_and_commit_state_tampering(self) -> None:
        from football_tracking.detector_probe import DetectorProbeCoordinator

        service = self._service()
        try:
            job = service.create_probe(self._request())
            observed = service.get_probe(job["job_id"])
        finally:
            service.close()

        cases = (
            lambda bundle: bundle["execution_environment"].pop("pydantic_version"),
            lambda bundle: bundle["execution_environment"].__setitem__("pydantic_core_version", ""),
            lambda bundle: bundle["execution_environment"].__setitem__("pydantic_version", "tampered"),
            lambda bundle: bundle.update(
                code_commit=None,
                code_commit_status="bound",
                code_commit_reason=None,
            ),
            lambda bundle: bundle.update(
                code_commit="a" * 40,
                code_commit_status="bound",
                code_commit_reason=None,
                code_commit_blob_files=dict(bundle["code_bundle_files"]),
                code_commit_blob_bundle_sha256="b" * 64,
                code_commit_binding_kind="exact_or_crlf_to_lf_commit_blob",
            ),
            lambda bundle: bundle.update(
                code_commit="a" * 40,
                code_commit_status="bound",
                code_commit_reason=None,
                code_commit_blob_files=dict(bundle["code_bundle_files"]),
                code_commit_blob_bundle_sha256=canonical_sha256(
                    bundle["code_bundle_files"]
                ),
                code_commit_binding_kind="raw_byte_equality",
            ),
            lambda bundle: bundle.update(
                code_commit=None,
                code_commit_status="unbound",
                code_commit_reason="repository_commit_unavailable",
            ),
            lambda bundle: bundle.update(
                code_commit=None,
                code_commit_status="dirty",
                code_commit_reason="code_bundle_differs_from_commit",
            ),
        )
        for mutate in cases:
            with self.subTest(mutate=mutate):
                frozen = deepcopy(observed["frozen_request"])
                mutate(frozen["execution_bundle"])
                with self.assertRaises(DetectorDevelopmentError):
                    DetectorProbeCoordinator._validate_frozen_execution_bundle(
                        frozen,
                        observed["frozen_profiles"],
                    )

    def test_source_hash_is_deferred_cached_and_visible_as_verifying_source(self) -> None:
        import football_tracking.detector_probe as probe_module

        original_hash = probe_module._hash_source_file_cancellable
        hash_calls: list[Path] = []
        entered = threading.Event()
        release = threading.Event()

        def counted_hash(repo_root, source_path, **kwargs):
            hash_calls.append(Path(source_path))
            entered.set()
            self.assertTrue(release.wait(5.0))
            return original_hash(repo_root, source_path, **kwargs)

        with probe_module._SOURCE_DIGEST_CACHE_LOCK:
            probe_module._SOURCE_DIGEST_CACHE.clear()
        service = self._service()
        worker: threading.Thread | None = None
        try:
            with patch.object(probe_module, "_hash_source_file_cancellable", side_effect=counted_hash):
                job = service.create_probe(self._request())
                self.assertEqual([], hash_calls, "POST/freeze must not hash source bytes")
                worker = threading.Thread(target=service.execute_probe, args=(job["job_id"],), daemon=True)
                worker.start()
                self.assertTrue(entered.wait(5.0))
                self.assertEqual("verifying_source", service.get_probe(job["job_id"])["stage"])
                release.set()
                worker.join(timeout=10.0)
                self.assertFalse(worker.is_alive())
                ready = service.get_probe(job["job_id"])
                self.assertEqual("ready", ready["status"])
                self.assertEqual(1, len(hash_calls))
                self.assertEqual(
                    ready["frozen_request"]["source_file_identity_sha256"],
                    ready["report"]["source"]["file_identity_sha256"],
                )

                retry = service.create_probe(self._request(retry_from_job_id=job["job_id"]))
                service.execute_probe(retry["job_id"])
                self.assertEqual("ready", service.get_probe(retry["job_id"])["status"])
                self.assertEqual(1, len(hash_calls), "exact retry must reuse verified bytes")
        finally:
            release.set()
            if worker is not None:
                worker.join(timeout=5.0)
            service.close()
            with probe_module._SOURCE_DIGEST_CACHE_LOCK:
                probe_module._SOURCE_DIGEST_CACHE.clear()

    def test_source_digest_mismatch_is_cached_and_blocks_exact_retry(self) -> None:
        import football_tracking.detector_probe as probe_module

        original_contract = self.contract.read_bytes()
        contract = json.loads(original_contract)
        contract["source"]["video_sha256"] = SHA_A
        _atomic_json(self.contract, contract)
        mismatched_contract_sha256 = _sha256(self.contract)
        original_hash = probe_module._hash_source_file_cancellable
        hash_calls = 0

        def counted_hash(*args, **kwargs):
            nonlocal hash_calls
            hash_calls += 1
            return original_hash(*args, **kwargs)

        with probe_module._SOURCE_DIGEST_CACHE_LOCK:
            probe_module._SOURCE_DIGEST_CACHE.clear()
        service = self._service()
        try:
            request = self._request(
                source_sha256=SHA_A,
                tracking_contract_sha256=mismatched_contract_sha256,
            )
            with patch.object(probe_module, "_hash_source_file_cancellable", side_effect=counted_hash):
                job = service.create_probe(request)
                self.assertEqual(0, hash_calls)
                service.execute_probe(job["job_id"])
                blocked = service.get_probe(job["job_id"])
                self.assertEqual("blocked", blocked["status"])
                self.assertEqual("source_digest_mismatch", blocked["blocker_code"])
                self.assertEqual("refresh_lineage", blocked["recovery_action"])
                self.assertEqual(1, hash_calls)

                retry = service.create_probe({**request, "retry_from_job_id": job["job_id"]})
                service.execute_probe(retry["job_id"])
                retry_blocked = service.get_probe(retry["job_id"])
                self.assertEqual("blocked", retry_blocked["status"])
                self.assertEqual("source_digest_mismatch", retry_blocked["blocker_code"])
                self.assertEqual(1, hash_calls)
        finally:
            service.close()
            self.contract.write_bytes(original_contract)
            with probe_module._SOURCE_DIGEST_CACHE_LOCK:
                probe_module._SOURCE_DIGEST_CACHE.clear()

    def test_cancel_during_source_hash_is_bounded_and_never_caches(self) -> None:
        import football_tracking.detector_probe as probe_module

        original_read = probe_module._read_source_hash_chunk
        entered = threading.Event()

        def slow_read(handle):
            entered.set()
            time.sleep(0.15)
            return original_read(handle)

        with probe_module._SOURCE_DIGEST_CACHE_LOCK:
            probe_module._SOURCE_DIGEST_CACHE.clear()
        service = self._service()
        worker: threading.Thread | None = None
        try:
            job = service.create_probe(self._request())
            with patch.object(probe_module, "_read_source_hash_chunk", side_effect=slow_read):
                worker = threading.Thread(target=service.execute_probe, args=(job["job_id"],), daemon=True)
                worker.start()
                self.assertTrue(entered.wait(5.0))
                self.assertEqual("verifying_source", service.get_probe(job["job_id"])["stage"])
                started = time.monotonic()
                service.cancel_probe(job["job_id"])
                worker.join(timeout=2.0)
                self.assertFalse(worker.is_alive())
                self.assertLess(time.monotonic() - started, 2.0)
            cancelled = service.get_probe(job["job_id"])
            self.assertEqual("cancelled", cancelled["status"])
            self.assertIsNone(cancelled["report"])
            with probe_module._SOURCE_DIGEST_CACHE_LOCK:
                self.assertEqual(0, len(probe_module._SOURCE_DIGEST_CACHE))
        finally:
            if worker is not None:
                worker.join(timeout=5.0)
            service.close()

    def test_close_during_source_hash_requeues_without_false_cancellation(self) -> None:
        import football_tracking.detector_probe as probe_module

        original_read = probe_module._read_source_hash_chunk
        entered = threading.Event()

        def slow_read(handle):
            entered.set()
            time.sleep(0.15)
            return original_read(handle)

        with probe_module._SOURCE_DIGEST_CACHE_LOCK:
            probe_module._SOURCE_DIGEST_CACHE.clear()
        service = self._service()
        job = service.create_probe(self._request())
        worker = threading.Thread(target=service.execute_probe, args=(job["job_id"],), daemon=True)
        try:
            with patch.object(probe_module, "_read_source_hash_chunk", side_effect=slow_read):
                worker.start()
                self.assertTrue(entered.wait(5.0))
                started = time.monotonic()
                service.close()
                self.assertLess(time.monotonic() - started, 2.0)
                worker.join(timeout=2.0)
                self.assertFalse(worker.is_alive())
            reopened = self._service()
            try:
                queued = reopened.get_probe(job["job_id"])
                self.assertEqual("queued", queued["status"])
                self.assertEqual("recovered_after_shutdown", queued["stage"])
                self.assertIsNone(queued["error_code"])
                self.assertIsNone(queued["report"])
            finally:
                reopened.close()
            with probe_module._SOURCE_DIGEST_CACHE_LOCK:
                self.assertEqual(0, len(probe_module._SOURCE_DIGEST_CACHE))
        finally:
            service.close()
            worker.join(timeout=5.0)

    def test_cancel_then_close_during_source_hash_preserves_terminal_cancel(self) -> None:
        import football_tracking.detector_probe as probe_module

        original_read = probe_module._read_source_hash_chunk
        entered = threading.Event()

        def slow_read(handle):
            entered.set()
            time.sleep(0.15)
            return original_read(handle)

        with probe_module._SOURCE_DIGEST_CACHE_LOCK:
            probe_module._SOURCE_DIGEST_CACHE.clear()
        service = self._service()
        job = service.create_probe(self._request())
        worker = threading.Thread(target=service.execute_probe, args=(job["job_id"],), daemon=True)
        with patch.object(probe_module, "_read_source_hash_chunk", side_effect=slow_read):
            worker.start()
            self.assertTrue(entered.wait(5.0))
            service.cancel_probe(job["job_id"])
            service.close()
            worker.join(timeout=2.0)
            self.assertFalse(worker.is_alive())

        observer = self._service()
        try:
            cancelled = observer.get_probe(job["job_id"])
        finally:
            observer.close()
        self.assertEqual("cancelled", cancelled["status"])
        self.assertEqual("cancelled", cancelled["error_code"])
        self.assertIsNone(cancelled["report"])
        with probe_module._SOURCE_DIGEST_CACHE_LOCK:
            self.assertEqual(0, len(probe_module._SOURCE_DIGEST_CACHE))

    def test_source_cache_invalidates_restored_mtime_and_ancestor_replacement(self) -> None:
        import football_tracking.detector_probe as probe_module

        with tempfile.TemporaryDirectory(dir=self.repo_root) as temporary:
            isolated_root = Path(temporary)
            data = isolated_root / "data"
            data.mkdir()
            source = data / "source.mp4"
            original = b"source-cache-fixture"
            changed = b"Source-cache-fixture"
            source.write_bytes(original)
            identity_one, size_one = probe_module._source_file_identity(isolated_root, source)
            calls = 0
            original_hash = probe_module._hash_source_file_cancellable

            def counted_hash(*args, **kwargs):
                nonlocal calls
                calls += 1
                return original_hash(*args, **kwargs)

            with probe_module._SOURCE_DIGEST_CACHE_LOCK:
                probe_module._SOURCE_DIGEST_CACHE.clear()
            with patch.object(probe_module, "_hash_source_file_cancellable", side_effect=counted_hash):
                probe_module._verify_source_digest_cached(
                    isolated_root,
                    source,
                    declared_sha256=hashlib.sha256(original).hexdigest(),
                    frozen_identity_sha256=identity_one,
                    frozen_size_bytes=size_one,
                    should_cancel=lambda: False,
                    should_shutdown=lambda: False,
                )
                first_stat = source.stat()
                source.write_bytes(changed)
                os.utime(
                    source,
                    ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns),
                )
                identity_two, size_two = probe_module._source_file_identity(isolated_root, source)
                self.assertNotEqual(identity_one, identity_two)
                probe_module._verify_source_digest_cached(
                    isolated_root,
                    source,
                    declared_sha256=hashlib.sha256(changed).hexdigest(),
                    frozen_identity_sha256=identity_two,
                    frozen_size_bytes=size_two,
                    should_cancel=lambda: False,
                    should_shutdown=lambda: False,
                )

                old_data = isolated_root / "data-old"
                data.rename(old_data)
                data.mkdir()
                source = data / "source.mp4"
                source.write_bytes(changed)
                os.utime(
                    source,
                    ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns),
                )
                identity_three, size_three = probe_module._source_file_identity(isolated_root, source)
                self.assertNotEqual(identity_two, identity_three)
                probe_module._verify_source_digest_cached(
                    isolated_root,
                    source,
                    declared_sha256=hashlib.sha256(changed).hexdigest(),
                    frozen_identity_sha256=identity_three,
                    frozen_size_bytes=size_three,
                    should_cancel=lambda: False,
                    should_shutdown=lambda: False,
                )
            self.assertEqual(3, calls)
            with probe_module._SOURCE_DIGEST_CACHE_LOCK:
                probe_module._SOURCE_DIGEST_CACHE.clear()

    def test_concurrent_source_cache_miss_hashes_once(self) -> None:
        import football_tracking.detector_probe as probe_module

        identity, size = probe_module._source_file_identity(self.repo_root, self.source)
        digest = _sha256(self.source)
        original_hash = probe_module._hash_source_file_cancellable
        calls = 0
        errors: list[BaseException] = []

        def counted_hash(*args, **kwargs):
            nonlocal calls
            calls += 1
            time.sleep(0.1)
            return original_hash(*args, **kwargs)

        def verify() -> None:
            try:
                probe_module._verify_source_digest_cached(
                    self.repo_root,
                    self.source,
                    declared_sha256=digest,
                    frozen_identity_sha256=identity,
                    frozen_size_bytes=size,
                    should_cancel=lambda: False,
                    should_shutdown=lambda: False,
                )
            except BaseException as exc:
                errors.append(exc)

        with probe_module._SOURCE_DIGEST_CACHE_LOCK:
            probe_module._SOURCE_DIGEST_CACHE.clear()
        with patch.object(probe_module, "_hash_source_file_cancellable", side_effect=counted_hash):
            workers = [threading.Thread(target=verify) for _ in range(4)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=5.0)
                self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(1, calls)
        with probe_module._SOURCE_DIGEST_CACHE_LOCK:
            probe_module._SOURCE_DIGEST_CACHE.clear()

    def test_cancel_after_source_hash_return_never_writes_positive_cache(self) -> None:
        import football_tracking.detector_probe as probe_module

        identity, size = probe_module._source_file_identity(self.repo_root, self.source)
        cancelled = threading.Event()
        original_hash = probe_module._hash_source_file_cancellable

        def cancel_on_return(*args, **kwargs):
            result = original_hash(*args, **kwargs)
            cancelled.set()
            return result

        with probe_module._SOURCE_DIGEST_CACHE_LOCK:
            probe_module._SOURCE_DIGEST_CACHE.clear()
        with patch.object(probe_module, "_hash_source_file_cancellable", side_effect=cancel_on_return):
            with self.assertRaisesRegex(DetectorDevelopmentError, "cancelled"):
                probe_module._verify_source_digest_cached(
                    self.repo_root,
                    self.source,
                    declared_sha256=_sha256(self.source),
                    frozen_identity_sha256=identity,
                    frozen_size_bytes=size,
                    should_cancel=cancelled.is_set,
                    should_shutdown=lambda: False,
                )
        with probe_module._SOURCE_DIGEST_CACHE_LOCK:
            self.assertEqual(0, len(probe_module._SOURCE_DIGEST_CACHE))

    def test_cancel_after_source_cache_insert_rolls_back_positive_cache(self) -> None:
        import football_tracking.detector_probe as probe_module

        identity, size = probe_module._source_file_identity(self.repo_root, self.source)
        abort_checks = 0

        def should_cancel() -> bool:
            nonlocal abort_checks
            abort_checks += 1
            return abort_checks >= 2

        with probe_module._SOURCE_DIGEST_CACHE_LOCK:
            probe_module._SOURCE_DIGEST_CACHE.clear()
        with patch.object(
            probe_module,
            "_hash_source_file_cancellable",
            return_value=(_sha256(self.source), size),
        ):
            with self.assertRaisesRegex(DetectorDevelopmentError, "cancelled"):
                probe_module._verify_source_digest_cached(
                    self.repo_root,
                    self.source,
                    declared_sha256=_sha256(self.source),
                    frozen_identity_sha256=identity,
                    frozen_size_bytes=size,
                    should_cancel=should_cancel,
                    should_shutdown=lambda: False,
                )
        self.assertEqual(2, abort_checks)
        with probe_module._SOURCE_DIGEST_CACHE_LOCK:
            self.assertEqual(0, len(probe_module._SOURCE_DIGEST_CACHE))

    def test_cached_digest_mismatch_with_cancel_prefers_terminal_cancel(self) -> None:
        import football_tracking.detector_probe as probe_module

        identity, size = probe_module._source_file_identity(self.repo_root, self.source)
        with probe_module._SOURCE_DIGEST_CACHE_LOCK:
            probe_module._SOURCE_DIGEST_CACHE.clear()
        probe_module._verify_source_digest_cached(
            self.repo_root,
            self.source,
            declared_sha256=_sha256(self.source),
            frozen_identity_sha256=identity,
            frozen_size_bytes=size,
            should_cancel=lambda: False,
            should_shutdown=lambda: False,
        )
        with self.assertRaises(DetectorDevelopmentError) as raised:
            probe_module._verify_source_digest_cached(
                self.repo_root,
                self.source,
                declared_sha256=SHA_A,
                frozen_identity_sha256=identity,
                frozen_size_bytes=size,
                should_cancel=lambda: True,
                should_shutdown=lambda: False,
            )
        self.assertEqual("cancelled", raised.exception.code)

    def test_source_hash_detects_same_size_restored_mtime_change_mid_read(self) -> None:
        import football_tracking.detector_probe as probe_module

        original = self.source.read_bytes()
        original_stat = self.source.stat()
        changed = bytes([original[0] ^ 1]) + original[1:]
        identity, size = probe_module._source_file_identity(self.repo_root, self.source)
        original_read = probe_module._read_source_hash_chunk
        mutated = False

        def mutate_after_read(handle):
            nonlocal mutated
            chunk = original_read(handle)
            if not mutated:
                mutated = True
                self.source.write_bytes(changed)
                os.utime(
                    self.source,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
            return chunk

        with probe_module._SOURCE_DIGEST_CACHE_LOCK:
            probe_module._SOURCE_DIGEST_CACHE.clear()
        try:
            with patch.object(probe_module, "_read_source_hash_chunk", side_effect=mutate_after_read):
                with self.assertRaisesRegex(DetectorDevelopmentError, "changed"):
                    probe_module._verify_source_digest_cached(
                        self.repo_root,
                        self.source,
                        declared_sha256=self.source_sha256,
                        frozen_identity_sha256=identity,
                        frozen_size_bytes=size,
                        should_cancel=lambda: False,
                        should_shutdown=lambda: False,
                    )
            with probe_module._SOURCE_DIGEST_CACHE_LOCK:
                self.assertEqual(0, len(probe_module._SOURCE_DIGEST_CACHE))
        finally:
            self.source.write_bytes(original)

    def test_source_hash_detects_ancestor_identity_change_mid_read(self) -> None:
        import football_tracking.detector_probe as probe_module

        original_identity = probe_module._stable_directory_object_identity
        original_read = probe_module._read_source_hash_chunk
        ancestor_changed = False

        def changed_ancestor(path):
            identity = original_identity(path)
            if ancestor_changed and Path(path) == self.source.parent:
                return identity[0], identity[1] + 1
            return identity

        def trigger_change(handle):
            nonlocal ancestor_changed
            chunk = original_read(handle)
            ancestor_changed = True
            return chunk

        identity, size = probe_module._source_file_identity(self.repo_root, self.source)
        with probe_module._SOURCE_DIGEST_CACHE_LOCK:
            probe_module._SOURCE_DIGEST_CACHE.clear()
        with (
            patch.object(
                probe_module,
                "_stable_directory_object_identity",
                side_effect=changed_ancestor,
            ),
            patch.object(probe_module, "_read_source_hash_chunk", side_effect=trigger_change),
        ):
            with self.assertRaisesRegex(DetectorDevelopmentError, "changed"):
                probe_module._verify_source_digest_cached(
                    self.repo_root,
                    self.source,
                    declared_sha256=self.source_sha256,
                    frozen_identity_sha256=identity,
                    frozen_size_bytes=size,
                    should_cancel=lambda: False,
                    should_shutdown=lambda: False,
                )
        with probe_module._SOURCE_DIGEST_CACHE_LOCK:
            self.assertEqual(0, len(probe_module._SOURCE_DIGEST_CACHE))

    def test_source_change_during_decode_blocks_post_run_publication(self) -> None:
        import football_tracking.detector_probe as probe_module

        original = self.source.read_bytes()
        original_stat = self.source.stat()
        changed = bytes([original[0] ^ 1]) + original[1:]

        def runner(request, profiles, staging, should_cancel, progress):
            output = self._successful_runner(request, profiles, staging, should_cancel, progress)
            self.source.write_bytes(changed)
            os.utime(
                self.source,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            return output

        with probe_module._SOURCE_DIGEST_CACHE_LOCK:
            probe_module._SOURCE_DIGEST_CACHE.clear()
        service = self._service(runner)
        try:
            job = service.create_probe(self._request())
            service.execute_probe(job["job_id"])
            blocked = service.get_probe(job["job_id"])
            results = self.repo_root / "data" / "ball_detector_development_v1" / "probes" / "results"
            self.assertFalse((results / job["job_id"]).exists())
            self.assertEqual([], list(results.glob(f".{job['job_id']}.staging-*")))
        finally:
            self.source.write_bytes(original)
            service.close()
            with probe_module._SOURCE_DIGEST_CACHE_LOCK:
                probe_module._SOURCE_DIGEST_CACHE.clear()
        self.assertEqual("blocked", blocked["status"])
        self.assertEqual("source_changed", blocked["blocker_code"])
        self.assertIsNone(blocked["report"])

    def test_source_identity_change_while_freezing_fails_without_hashing(self) -> None:
        import football_tracking.detector_probe as probe_module

        original_identity = probe_module._source_file_identity
        identity_calls = 0
        hash_calls = 0

        def changing_identity(repo_root, source_path):
            nonlocal identity_calls
            identity, size = original_identity(repo_root, source_path)
            if Path(source_path) == self.source:
                identity_calls += 1
                if identity_calls == 2:
                    identity = ("0" if identity[0] != "0" else "1") + identity[1:]
            return identity, size

        def unexpected_hash(*args, **kwargs):
            nonlocal hash_calls
            hash_calls += 1
            raise AssertionError("freeze must not hash source bytes")

        service = self._service()
        try:
            with (
                patch.object(probe_module, "_source_file_identity", side_effect=changing_identity),
                patch.object(probe_module, "_hash_source_file_cancellable", side_effect=unexpected_hash),
            ):
                with self.assertRaisesRegex(DetectorDevelopmentError, "changed"):
                    service.create_probe(self._request())
        finally:
            service.close()
        self.assertEqual(2, identity_calls)
        self.assertEqual(0, hash_calls)

    def test_runtime_evidence_change_requires_new_root_and_exact_retry(self) -> None:
        catalog = self._catalog()
        service = DetectorDevelopmentService(
            self.repo_root,
            probe_runner=self._successful_runner,
            auto_start_workers=False,
            catalog_provider=lambda: deepcopy(catalog),
        )
        try:
            first = service.create_probe(self._request())
            service.execute_probe(first["job_id"])
            first_ready = service.get_probe(first["job_id"])
            catalog["models"][0]["availability"]["observations"]["runtime_load"]["evidence_sha256"] = SHA_B

            with self.assertRaisesRegex(DetectorDevelopmentError, "exact frozen intent"):
                service.create_probe(self._request(retry_from_job_id=first["job_id"]))
            second = service.create_probe(self._request())
            second_status = service.get_probe(second["job_id"])
        finally:
            service.close()

        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertNotEqual(first_ready["intent_sha256"], second_status["intent_sha256"])
        self.assertNotEqual(
            first_ready["frozen_request"]["runtime_environment_sha256"],
            second_status["frozen_request"]["runtime_environment_sha256"],
        )

    def test_active_job_is_not_reused_after_runtime_environment_change(self) -> None:
        catalog = self._catalog()
        service = DetectorDevelopmentService(
            self.repo_root,
            probe_runner=self._successful_runner,
            auto_start_workers=False,
            catalog_provider=lambda: deepcopy(catalog),
        )
        try:
            first = service.create_probe(self._request())
            catalog["models"][0]["availability"]["observations"]["runtime_load"]["evidence_sha256"] = SHA_B
            with self.assertRaisesRegex(DetectorDevelopmentError, "conflicting"):
                service.create_probe(self._request())
            observed = service.get_probe(first["job_id"])
        finally:
            service.close()

        self.assertEqual("queued", observed["status"])

    def test_execution_blocks_when_code_bundle_or_hardware_binding_drifts(self) -> None:
        import football_tracking.detector_probe as probe_module

        original_read = probe_module.read_regular_bytes
        changed_files = (
            "__init__.py",
            "api/__init__.py",
            "api/schemas.py",
            "ai_contracts.py",
            "ai_improvement_prompt_contract.py",
            "config.py",
            "detector.py",
            "media_integrity.py",
        )
        package_root = Path(probe_module.__file__).resolve().parent
        for index, changed_name in enumerate(changed_files, start=1):
            with self.subTest(changed_name=changed_name):
                service = self._service()
                try:
                    job = service.create_probe(self._request(trial_intent_sha256=f"{index + 10:064x}"))

                    def changed_read(path, *args, **kwargs):
                        content, digest = original_read(path, *args, **kwargs)
                        try:
                            relative_path = (
                                Path(path).resolve().relative_to(package_root).as_posix()
                            )
                        except ValueError:
                            return content, digest
                        if relative_path == changed_name:
                            content += b"# simulated source drift\n"
                            digest = hashlib.sha256(content).hexdigest()
                        return content, digest

                    with patch.object(
                        probe_module,
                        "read_regular_bytes",
                        side_effect=changed_read,
                    ):
                        service.execute_probe(job["job_id"])
                    blocked = service.get_probe(job["job_id"])
                finally:
                    service.close()

                self.assertEqual("blocked", blocked["status"])
                self.assertEqual("runtime_environment_changed", blocked["blocker_code"])

        service = self._service()
        try:
            job = service.create_probe(self._request(trial_intent_sha256="f" * 64))
            frozen_environment = service.get_probe(job["job_id"])["frozen_request"]["execution_bundle"][
                "execution_environment"
            ]
            changed_environment = deepcopy(frozen_environment)
            changed_environment["cuda_visible_devices"] = "drifted"
            with patch.object(
                probe_module,
                "probe_execution_environment",
                return_value=changed_environment,
            ):
                service.execute_probe(job["job_id"])
            blocked = service.get_probe(job["job_id"])
        finally:
            service.close()

        self.assertEqual("blocked", blocked["status"])
        self.assertEqual("runtime_environment_changed", blocked["blocker_code"])

        for field in ("pydantic_version", "pydantic_core_version"):
            with self.subTest(field=field):
                service = self._service()
                try:
                    job = service.create_probe(
                        self._request(trial_intent_sha256=canonical_sha256({"field": field}))
                    )
                    changed_environment = deepcopy(
                        service.get_probe(job["job_id"])["frozen_request"]["execution_bundle"][
                            "execution_environment"
                        ]
                    )
                    changed_environment[field] = "version-drift"
                    with patch.object(
                        probe_module,
                        "probe_execution_environment",
                        return_value=changed_environment,
                    ):
                        service.execute_probe(job["job_id"])
                    blocked = service.get_probe(job["job_id"])
                finally:
                    service.close()
                self.assertEqual("blocked", blocked["status"])
                self.assertEqual("runtime_environment_changed", blocked["blocker_code"])

        service = self._service()
        try:
            job = service.create_probe(self._request(trial_intent_sha256="e" * 64))
            changed_environment = deepcopy(
                service.get_probe(job["job_id"])["frozen_request"]["execution_bundle"]["execution_environment"]
            )
            changed_environment["opencv_version"] = "decoder-drift"
            decoder_fingerprint = {
                key: changed_environment[key]
                for key in (
                    "python_implementation",
                    "python_version",
                    "numpy_version",
                    "opencv_version",
                    "opencv_build_information_sha256",
                    "opencv_ffmpeg_enabled",
                )
            }
            changed_environment["decoder_fingerprint_sha256"] = canonical_sha256(decoder_fingerprint)
            with patch.object(
                probe_module,
                "probe_execution_environment",
                return_value=changed_environment,
            ):
                service.execute_probe(job["job_id"])
            blocked = service.get_probe(job["job_id"])
        finally:
            service.close()

        self.assertEqual("blocked", blocked["status"])
        self.assertEqual("runtime_environment_changed", blocked["blocker_code"])

    def test_two_coordinators_create_one_content_idempotent_job(self) -> None:
        first = self._service()
        second = self._service()
        barrier = threading.Barrier(3)
        created: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def create(service: DetectorDevelopmentService) -> None:
            try:
                barrier.wait()
                created.append(service.create_probe(self._request()))
            except BaseException as exc:
                errors.append(exc)

        workers = [threading.Thread(target=create, args=(service,)) for service in (first, second)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(5)
        try:
            self.assertEqual([], errors)
            self.assertEqual(2, len(created))
            self.assertEqual(created[0]["job_id"], created[1]["job_id"])
            jobs = list((self.repo_root / "data" / "ball_detector_development_v1" / "probes" / "jobs").glob("*.json"))
            self.assertEqual(1, len(jobs))
        finally:
            first.close()
            second.close()

    def test_two_spawned_processes_create_one_content_idempotent_job(self) -> None:
        script = r"""
import json
import sys
import time
from pathlib import Path

from football_tracking.detector_development import DetectorDevelopmentService

root = Path(sys.argv[1])
request = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
catalog = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
ready = Path(sys.argv[4])
start = Path(sys.argv[5])
service = DetectorDevelopmentService(
    root,
    auto_start_workers=False,
    catalog_provider=lambda: catalog,
)
try:
    ready.write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 10
    while not start.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("process barrier timed out")
        time.sleep(0.01)
    print(service.create_probe(request)["job_id"], flush=True)
finally:
    service.close()
"""
        with tempfile.TemporaryDirectory(dir=self.repo_root) as sync_name:
            sync_root = Path(sync_name)
            request_path = sync_root / "request.json"
            catalog_path = sync_root / "catalog.json"
            start_path = sync_root / "start"
            _atomic_json(request_path, self._request())
            _atomic_json(catalog_path, self._catalog())
            processes: list[subprocess.Popen[str]] = []
            ready_paths: list[Path] = []
            for index in range(2):
                ready_path = sync_root / f"ready-{index}"
                ready_paths.append(ready_path)
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            script,
                            str(self.repo_root),
                            str(request_path),
                            str(catalog_path),
                            str(ready_path),
                            str(start_path),
                        ],
                        cwd=Path(__file__).resolve().parents[1],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            deadline = time.monotonic() + 10
            while not all(path.exists() for path in ready_paths):
                if any(process.poll() is not None for process in processes):
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            self.assertTrue(all(path.exists() for path in ready_paths))
            start_path.write_text("start", encoding="utf-8")
            outputs: list[str] = []
            try:
                for process in processes:
                    stdout, stderr = process.communicate(timeout=15)
                    self.assertEqual(0, process.returncode, stderr)
                    outputs.append(stdout.strip())
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)

        self.assertEqual(2, len(outputs))
        self.assertEqual(outputs[0], outputs[1])
        jobs = list((self.repo_root / "data" / "ball_detector_development_v1" / "probes" / "jobs").glob("*.json"))
        self.assertEqual(1, len(jobs))

    def test_global_execution_slot_runs_distinct_jobs_in_durable_order(self) -> None:
        guard = threading.Lock()
        first_started = threading.Event()
        release_first = threading.Event()
        active = 0
        maximum_active = 0
        order: list[str] = []

        def runner(request, profiles, staging, should_cancel, progress):
            nonlocal active, maximum_active
            with guard:
                active += 1
                maximum_active = max(maximum_active, active)
                order.append(request["trial_intent_sha256"])
                ordinal = len(order)
            try:
                if ordinal == 1:
                    first_started.set()
                    self.assertTrue(release_first.wait(5))
                return self._successful_runner(request, profiles, staging, should_cancel, progress)
            finally:
                with guard:
                    active -= 1

        first = self._service(runner, auto_start=True)
        second = self._service(runner, auto_start=True)
        try:
            first_job = first.create_probe(self._request(trial_intent_sha256=SHA_A))
            self.assertTrue(first_started.wait(5))
            second_job = second.create_probe(self._request(trial_intent_sha256=SHA_B))
            self.assertEqual("queued", second.get_probe(second_job["job_id"])["status"])
            release_first.set()
            self._wait_status(first, first_job["job_id"], {"ready"})
            self._wait_status(second, second_job["job_id"], {"ready"})
            self.assertEqual(1, maximum_active)
            self.assertEqual([SHA_A, SHA_B], order)
        finally:
            release_first.set()
            first.close()
            second.close()

    def test_closing_waiting_peer_does_not_mutate_the_active_or_queued_job(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def runner(request, profiles, staging, should_cancel, progress):
            if request["trial_intent_sha256"] == SHA_A:
                entered.set()
                self.assertTrue(release.wait(5))
            return self._successful_runner(request, profiles, staging, should_cancel, progress)

        active = self._service(runner, auto_start=True)
        waiting = self._service(runner, auto_start=True)
        first_job = active.create_probe(self._request(trial_intent_sha256=SHA_A))
        self.assertTrue(entered.wait(5))
        second_job = waiting.create_probe(self._request(trial_intent_sha256=SHA_B))
        closer = threading.Thread(target=waiting.close)
        closer.start()
        time.sleep(0.05)
        queued = active.get_probe(second_job["job_id"])
        self.assertEqual("queued", queued["status"])
        self.assertNotEqual("cancelled", queued.get("error_code"))
        release.set()
        closer.join(5)
        self.assertFalse(closer.is_alive())
        try:
            self._wait_status(active, first_job["job_id"], {"ready"})
            self._wait_status(active, second_job["job_id"], {"ready"})
        finally:
            release.set()
            active.close()

    def test_shutdown_requeues_running_job_without_forging_operator_cancellation(self) -> None:
        entered = threading.Event()

        def runner(_request, _profiles, _staging, should_cancel, _progress):
            entered.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                should_cancel()
                time.sleep(0.005)
            raise AssertionError("shutdown did not reach a cooperative boundary")

        first = self._service(runner, auto_start=True)
        job = first.create_probe(self._request(trial_intent_sha256=SHA_A))
        self.assertTrue(entered.wait(5))
        first.close()

        second = self._service(auto_start=False)
        try:
            recovered = second.get_probe(job["job_id"])
            self.assertEqual("queued", recovered["status"])
            self.assertEqual("recovered_after_shutdown", recovered["stage"])
            self.assertIsNone(recovered["error_code"])
            self.assertTrue(recovered["can_cancel"])
        finally:
            second.close()

    def test_live_peer_polls_later_jobs_and_preserves_dead_owner_cancellation(self) -> None:
        creator = self._service()
        peer = self._service(auto_start=True)
        creator_coordinator = creator._probes()
        peer._probes()
        try:
            discovered = creator.create_probe(self._request(trial_intent_sha256=SHA_A))
            self._wait_status(peer, discovered["job_id"], {"ready"})

            with creator_coordinator._lock:
                cancelled = creator.create_probe(self._request(trial_intent_sha256=SHA_B))
                record = creator_coordinator._record(cancelled["job_id"])
                record.update(
                    {
                        "status": "running",
                        "stage": "inference",
                        "owner_id": "dead-owner",
                        "cancel_requested": True,
                    }
                )
                record["progress"]["completed"] = 1
                creator_coordinator._persist_record(record)
            terminal = self._wait_status(peer, cancelled["job_id"], {"cancelled"})
            self.assertEqual("cancelled", terminal["status"])
            self.assertEqual("cancelled", terminal["error_code"])
        finally:
            creator.close()
            peer.close()

    def test_profile_count_top_k_paths_digests_contract_and_source_are_fail_closed(self) -> None:
        service = self._service()
        invalid = [
            self._request(profile_ids=["official-coco-yolo11n-direct"]),
            self._request(profile_ids=["official-coco-yolo11n-direct"] * 7),
            self._request(top_k=4),
            self._request(source_relative_path="../source.mp4"),
            self._request(source_relative_path=str(self.source.resolve())),
            self._request(source_sha256=SHA_C),
            self._request(tracking_contract_sha256=SHA_C),
            self._request(tracking_contract_relative_path="data/source.mp4"),
            self._request(frame_indices=list(range(51))),
            self._request(profile_ids=["official-coco-yolo11n-direct", "unknown"]),
        ]
        try:
            for request in invalid:
                with self.subTest(request=request), self.assertRaises(DetectorDevelopmentError):
                    service.create_probe(request)
        finally:
            service.close()

    def test_active_intent_is_idempotent_conflict_is_blocked_and_terminal_requires_retry(self) -> None:
        service = self._service()
        try:
            first = service.create_probe(self._request())
            same = service.create_probe(self._request())
            self.assertEqual(first["job_id"], same["job_id"])

            with self.assertRaisesRegex(DetectorDevelopmentError, "active"):
                service.create_probe(
                    self._request(
                        profile_ids=["official-coco-yolo11n-direct", "official-coco-yolo11n-sahi"], frame_indices=[4, 8]
                    )
                )

            service.execute_probe(first["job_id"])
            ready = service.get_probe(first["job_id"])
            self.assertEqual("ready", ready["status"])
            with self.assertRaisesRegex(DetectorDevelopmentError, "retry"):
                service.create_probe(self._request())

            retry = service.create_probe(self._request(retry_from_job_id=first["job_id"]))
            self.assertNotEqual(first["job_id"], retry["job_id"])
            self.assertEqual(first["job_id"], retry["retry_from_job_id"])
            service.execute_probe(retry["job_id"])
            replayed_retry = service.create_probe(self._request(retry_from_job_id=first["job_id"]))
            self.assertEqual(retry["job_id"], replayed_retry["job_id"])
            next_retry = service.create_probe(self._request(retry_from_job_id=retry["job_id"]))
            self.assertNotEqual(retry["job_id"], next_retry["job_id"])
        finally:
            service.close()

    def test_ready_report_is_atomic_source_bound_and_has_per_frame_evidence(self) -> None:
        service = self._service()
        try:
            job = service.create_probe(self._request())
            service.execute_probe(job["job_id"])
            ready = service.get_probe(job["job_id"])
            report = ready["report"]
            frame_path, media_type, digest = service.get_probe_artifact(job["job_id"], "source-frame-000000003")
            overlay_path, overlay_media_type, overlay_digest = service.get_probe_artifact(
                job["job_id"], "raw-overlay-000000003-official-coco-yolo11n-direct"
            )
        finally:
            service.close()

        self.assertEqual("ready", ready["status"])
        self.assertEqual("detector_probe_report", report["artifact_type"])
        self.assertEqual(self.source_sha256, report["source"]["sha256"])
        self.assertEqual(self.contract_sha256, report["source"]["tracking_contract_sha256"])
        self.assertEqual(self.base_config_sha256, report["lineage"]["base_config_sha256"])
        self.assertEqual(self.effective_config_sha256, report["lineage"]["effective_config_sha256"])
        self.assertEqual(SHA_C, report["lineage"]["trial_intent_sha256"])
        self.assertEqual(EMPTY_TUNING_BINDING, report["lineage"]["tuning_patch_binding"])
        self.assertEqual(EMPTY_TUNING_SHA256, report["lineage"]["tuning_patch_sha256"])
        self.assertEqual(
            ready["frozen_profiles_sha256"],
            report["lineage"]["frozen_profiles_sha256"],
        )
        self.assertEqual(
            ready["frozen_request"]["execution_bundle"],
            report["lineage"]["execution_bundle"],
        )
        self.assertEqual(
            ready["frozen_request"]["execution_bundle_sha256"],
            report["lineage"]["execution_bundle_sha256"],
        )
        self.assertEqual([3, 6, 12], [item["frame_index"] for item in report["frames"]])
        frame = report["frames"][0]
        self.assertEqual(
            f"/api/v1/detector-probes/{job['job_id']}/artifacts/source-frame-000000003",
            frame["source_artifact_url"],
        )
        self.assertGreater(frame["source_frame_size_bytes"], 0)
        self.assertEqual(2, len(frame["profile_results"]))
        self.assertEqual(5, frame["profile_results"][0]["top_k"])
        self.assertTrue(frame["profile_results"][0]["display_candidate"])
        self.assertRegex(frame["profile_results"][0]["raw_overlay_sha256"], r"^[0-9a-f]{64}$")
        self.assertGreater(frame["profile_results"][0]["raw_overlay_size_bytes"], 0)
        self.assertEqual(
            f"/api/v1/detector-probes/{job['job_id']}/artifacts/raw-overlay-000000003-official-coco-yolo11n-direct",
            frame["profile_results"][0]["raw_overlay_artifact_url"],
        )
        self.assertEqual("image/jpeg", media_type)
        self.assertEqual(frame["source_frame_sha256"], digest)
        self.assertEqual(_jpeg_fixture(), frame_path.read_bytes())
        self.assertEqual("image/jpeg", overlay_media_type)
        self.assertEqual(frame["profile_results"][0]["raw_overlay_sha256"], overlay_digest)
        self.assertEqual(_jpeg_fixture(), overlay_path.read_bytes())
        self.assertRegex(report["report_sha256"], r"^[0-9a-f]{64}$")

        with self.assertRaises(DetectorDevelopmentError):
            service = self._service()
            try:
                service.get_probe_artifact(job["job_id"], "../../source.mp4")
            finally:
                service.close()

        result_root = self.repo_root / "data" / "ball_detector_development_v1" / "probes" / "results" / job["job_id"]
        manifest = json.loads((result_root / "detector_probe_manifest.v1.json").read_text(encoding="utf-8"))
        self.assertEqual(ready["frozen_profiles_sha256"], manifest["frozen_profiles_sha256"])
        self.assertEqual(
            ready["frozen_request"]["execution_bundle_sha256"],
            manifest["execution_bundle_sha256"],
        )
        self.assertEqual(
            ready["frozen_request"]["runtime_environment_sha256"],
            manifest["runtime_environment_sha256"],
        )
        self.assertTrue((result_root / "detector_probe_report.v1.json").is_file())
        self.assertFalse(any(result_root.parent.glob(f".{job['job_id']}.staging-*")))

    def test_artifact_get_reads_and_decodes_only_the_requested_allowlisted_image(self) -> None:
        import football_tracking.detector_probe as probe_module

        service = self._service()
        try:
            job = service.create_probe(self._request())
            service.execute_probe(job["job_id"])
            original_read = probe_module.read_regular_bytes
            with patch.object(
                probe_module,
                "read_regular_bytes",
                wraps=original_read,
            ) as read_bytes:
                path, _media_type, _digest = service.get_probe_artifact(job["job_id"], "source-frame-000000003")
        finally:
            service.close()

        image_reads = [
            Path(call.args[0]).resolve()
            for call in read_bytes.call_args_list
            if Path(call.args[0]).suffix.lower() == ".jpg"
        ]
        self.assertEqual([path.resolve()], image_reads)
        self.assertFalse(any("overlays" in candidate.parts for candidate in image_reads))

    def test_concurrent_artifact_gets_do_not_hold_the_registry_lock_during_decode(self) -> None:
        service = self._service()
        try:
            job = service.create_probe(self._request())
            service.execute_probe(job["job_id"])
            coordinator = service._probes()
            original = coordinator._validate_result_artifact
            guard = threading.Lock()
            active = 0
            maximum_active = 0

            def observed(*args, **kwargs):
                nonlocal active, maximum_active
                with guard:
                    active += 1
                    maximum_active = max(maximum_active, active)
                try:
                    time.sleep(0.025)
                    return original(*args, **kwargs)
                finally:
                    with guard:
                        active -= 1

            errors: list[BaseException] = []

            def fetch() -> None:
                try:
                    service.get_probe_artifact(job["job_id"], "source-frame-000000003")
                except BaseException as exc:
                    errors.append(exc)

            with patch.object(
                coordinator,
                "_validate_result_artifact",
                side_effect=observed,
            ):
                workers = [threading.Thread(target=fetch) for _ in range(8)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(5)
        finally:
            service.close()

        self.assertEqual([], errors)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertGreater(maximum_active, 1)
        self.assertLessEqual(maximum_active, 3)

    def test_concurrent_artifact_delivery_bounds_the_actual_image_reads(self) -> None:
        import football_tracking.detector_probe as probe_module

        service = self._service()
        try:
            job = service.create_probe(self._request())
            service.execute_probe(job["job_id"])
            original_read = probe_module.read_regular_bytes
            guard = threading.Lock()
            active = 0
            maximum_active = 0

            def observed(path, *args, **kwargs):
                nonlocal active, maximum_active
                if Path(path).suffix.lower() != ".jpg":
                    return original_read(path, *args, **kwargs)
                with guard:
                    active += 1
                    maximum_active = max(maximum_active, active)
                try:
                    time.sleep(0.025)
                    return original_read(path, *args, **kwargs)
                finally:
                    with guard:
                        active -= 1

            errors: list[BaseException] = []

            def fetch() -> None:
                try:
                    content, media_type, digest = service.read_probe_artifact(job["job_id"], "source-frame-000000003")
                    self.assertEqual(_jpeg_fixture(), content)
                    self.assertEqual("image/jpeg", media_type)
                    self.assertRegex(digest, r"^[0-9a-f]{64}$")
                except BaseException as exc:
                    errors.append(exc)

            with patch.object(
                probe_module,
                "read_regular_bytes",
                side_effect=observed,
            ):
                workers = [threading.Thread(target=fetch) for _ in range(8)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(5)
        finally:
            service.close()

        self.assertEqual([], errors)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertGreater(maximum_active, 1)
        self.assertLessEqual(maximum_active, 3)

    def test_repeated_artifact_gets_reuse_the_digest_bound_report_index(self) -> None:
        service = self._service()
        try:
            job = service.create_probe(self._request())
            service.execute_probe(job["job_id"])
            coordinator = service._probes()
            with patch.object(
                coordinator,
                "_read_result_documents",
                wraps=coordinator._read_result_documents,
            ) as read_documents:
                service.get_probe_artifact(job["job_id"], "source-frame-000000003")
                service.get_probe_artifact(
                    job["job_id"],
                    "raw-overlay-000000003-official-coco-yolo11n-direct",
                )
        finally:
            service.close()

        self.assertEqual(1, read_documents.call_count)

    def test_artifact_index_cache_invalidates_when_report_identity_changes(self) -> None:
        service = self._service()
        try:
            job = service.create_probe(self._request())
            service.execute_probe(job["job_id"])
            coordinator = service._probes()
            result_root = coordinator._results_root / job["job_id"]
            report_path = result_root / "detector_probe_report.v1.json"
            original_stat = report_path.stat()
            original = report_path.read_bytes()
            with patch.object(
                coordinator,
                "_read_result_documents",
                wraps=coordinator._read_result_documents,
            ) as read_documents:
                service.get_probe_artifact(job["job_id"], "source-frame-000000003")
                tampered = bytearray(original)
                tampered[len(tampered) // 2] ^= 1
                report_path.write_bytes(tampered)
                os.utime(
                    report_path,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
                with self.assertRaises(DetectorDevelopmentError):
                    service.get_probe_artifact(job["job_id"], "source-frame-000000003")
        finally:
            service.close()

        self.assertEqual(2, read_documents.call_count)

    def test_queued_cancel_is_terminal_and_retryable(self) -> None:
        service = self._service()
        try:
            job = service.create_probe(self._request())
            cancelled = service.cancel_probe(job["job_id"])
            self.assertEqual("cancelled", cancelled["status"])
            self.assertFalse(cancelled["can_cancel"])
            with self.assertRaisesRegex(DetectorDevelopmentError, "retry"):
                service.create_probe(self._request())
            retry = service.create_probe(self._request(retry_from_job_id=job["job_id"]))
            self.assertEqual(job["job_id"], retry["retry_from_job_id"])
        finally:
            service.close()

    def test_cancellation_after_commit_start_is_rejected_and_commit_wins(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocking_publish(staging: Path, destination: Path) -> None:
            entered.set()
            self.assertTrue(release.wait(5))
            os.replace(staging, destination)

        service = self._service(auto_start=True)
        with patch("football_tracking.detector_probe._publish_staging_directory", side_effect=blocking_publish):
            try:
                job = service.create_probe(self._request())
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(DetectorDevelopmentError, "commit"):
                    service.cancel_probe(job["job_id"])
                release.set()
                ready = self._wait_terminal(service, job["job_id"])
                self.assertEqual("ready", ready["status"])
            finally:
                release.set()
                service.close()

    def test_running_cancel_is_cooperative_and_never_commits(self) -> None:
        entered = threading.Event()

        def runner(_request, _profiles, _staging, should_cancel, _progress):
            entered.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if should_cancel():
                    raise DetectorDevelopmentError("cancelled", "cancelled")
                time.sleep(0.005)
            raise AssertionError("running detector probe did not receive cancellation")

        service = self._service(runner, auto_start=True)
        try:
            job = service.create_probe(self._request())
            self.assertTrue(entered.wait(5))
            cancelling = service.cancel_probe(job["job_id"])
            self.assertEqual("running", cancelling["status"])
            terminal = self._wait_terminal(service, job["job_id"])
        finally:
            service.close()
        self.assertEqual("cancelled", terminal["status"])
        result = self.repo_root / "data" / "ball_detector_development_v1" / "probes" / "results" / job["job_id"]
        self.assertFalse(result.exists())

    def test_durable_cancel_wins_when_failure_records_after_cancel_commit(self) -> None:
        service = self._service()
        try:
            job = service.create_probe(self._request())
            coordinator = service._probes()
            claimed = coordinator._claim_probe(job["job_id"])
            self.assertIsNotNone(claimed)
            cancelling = service.cancel_probe(job["job_id"])
            self.assertEqual("running", cancelling["status"])
            coordinator._record_failure(
                job["job_id"],
                DetectorDevelopmentError("source_digest_mismatch", "injected failure after cancel commit"),
            )
            terminal = service.get_probe(job["job_id"])
        finally:
            service.close()
        self.assertEqual("cancelled", terminal["status"])
        self.assertEqual("cancelled", terminal["error_code"])
        self.assertIsNone(terminal["blocker_code"])

    def test_slow_catalog_io_does_not_block_probe_status_cancel_or_close(self) -> None:
        import football_tracking.detector_development as development

        runner_entered = threading.Event()
        catalog_entered = threading.Event()
        release_catalog = threading.Event()

        def runner(_request, _profiles, _staging, should_cancel, _progress):
            runner_entered.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if should_cancel():
                    raise DetectorDevelopmentError("cancelled", "cancelled")
                time.sleep(0.005)
            raise AssertionError("probe cancellation was blocked by catalog I/O")

        original_catalog = development.build_builtin_model_catalog

        def slow_catalog(root):
            catalog_entered.set()
            if not release_catalog.wait(5):
                raise AssertionError("slow catalog test timed out")
            return original_catalog(root)

        service = self._service(runner, auto_start=True)
        errors: list[BaseException] = []
        try:
            job = service.create_probe(self._request())
            self.assertTrue(runner_entered.wait(5))
            with patch.object(development, "build_builtin_model_catalog", side_effect=slow_catalog):
                catalog_worker = threading.Thread(target=lambda: _capture_thread_error(service.list_models, errors))
                catalog_worker.start()
                self.assertTrue(catalog_entered.wait(5))

                started = time.monotonic()
                self.assertEqual("running", service.get_probe(job["job_id"])["status"])
                service.cancel_probe(job["job_id"])
                self.assertLess(time.monotonic() - started, 1.0)

                closer = threading.Thread(target=service.close)
                closer.start()
                closer.join(2)
                self.assertFalse(closer.is_alive())
                release_catalog.set()
                catalog_worker.join(5)
        finally:
            release_catalog.set()
            service.close()

        self.assertFalse(catalog_worker.is_alive())
        self.assertEqual([], errors)

    def test_decode_cancel_polling_reads_only_the_target_job_token(self) -> None:
        service = self._service()
        try:
            job = service.create_probe(self._request())
            coordinator = service._probes()
            with patch.object(
                coordinator,
                "_refresh_jobs_from_disk",
                side_effect=AssertionError("cancel polling scanned historical jobs"),
            ):
                for _ in range(4096):
                    self.assertFalse(coordinator._cancellation_requested(job["job_id"]))
            service.cancel_probe(job["job_id"])
            self.assertTrue(coordinator._cancellation_requested(job["job_id"]))
        finally:
            service.close()

    def test_large_history_and_unrelated_corrupt_job_do_not_break_healthy_job(self) -> None:
        service = self._service()
        try:
            job = service.create_probe(self._request())
            coordinator = service._probes()
            source_record = json.loads((coordinator._jobs_root / f"{job['job_id']}.json").read_text(encoding="utf-8"))
            copied_job_id = "probe-history-0000"
            for index in range(256):
                copied = deepcopy(source_record)
                copied["job_id"] = f"probe-history-{index:04d}"
                _atomic_json(
                    coordinator._jobs_root / f"probe-history-{index:04d}.json",
                    copied,
                )
            (coordinator._jobs_root / "unrelated-corrupt.json").write_bytes(b"{not-json")

            with patch.object(
                coordinator,
                "_refresh_jobs_from_disk",
                side_effect=AssertionError("targeted job read scanned job history"),
            ):
                self.assertEqual("queued", service.get_probe(job["job_id"])["status"])
                self.assertFalse(coordinator._cancellation_requested(job["job_id"]))
                self.assertEqual("cancelled", service.cancel_probe(copied_job_id)["status"])

            service.execute_probe(job["job_id"])
            self.assertEqual("ready", service.get_probe(job["job_id"])["status"])
        finally:
            service.close()

    def test_live_owner_lease_prevents_another_service_from_recovering_running_job(self) -> None:
        entered = threading.Event()

        def runner(_request, _profiles, _staging, should_cancel, _progress):
            entered.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if should_cancel():
                    raise DetectorDevelopmentError("cancelled", "cancelled")
                time.sleep(0.005)
            raise AssertionError("live-owner test timed out")

        first = self._service(runner, auto_start=True)
        second = self._service(auto_start=False)
        try:
            job = first.create_probe(self._request())
            self.assertTrue(entered.wait(5))
            observed = second.get_probe(job["job_id"])
            self.assertEqual("running", observed["status"])
            self.assertNotEqual("recovered_after_restart", observed["stage"])
            first.cancel_probe(job["job_id"])
            self.assertEqual("cancelled", self._wait_terminal(first, job["job_id"])["status"])
        finally:
            second.close()
            first.close()

    def test_resource_and_worker_failures_publish_no_partial_report(self) -> None:
        import torch

        failures = [
            (MemoryError("CUDA out of memory"), "device_out_of_memory"),
            (torch.cuda.OutOfMemoryError("CUDA out of memory"), "device_out_of_memory"),
            (OSError(errno.ENOSPC, "disk full"), "disk_exhausted"),
            (ProbeWorkerDiedError("worker exited"), "worker_died"),
            (CorruptProbeFrameError("cannot decode frame"), "corrupt_frame"),
            (ArtifactWriteError("cannot write evidence"), "artifact_write_failed"),
            (RuntimeError("unexpected"), "probe_failed"),
        ]
        for index, (failure, expected_code) in enumerate(failures):
            with self.subTest(expected_code=expected_code):

                def runner(*_args, failure=failure, **_kwargs):
                    raise failure

                service = self._service(runner)
                try:
                    request = self._request(trial_intent_sha256=f"{index + 1:064x}")
                    job = service.create_probe(request)
                    service.execute_probe(job["job_id"])
                    failed = service.get_probe(job["job_id"])
                finally:
                    service.close()
                self.assertEqual("failed", failed["status"])
                self.assertEqual(expected_code, failed["error_code"])
                self.assertIsNone(failed["report"])
                result = self.repo_root / "data" / "ball_detector_development_v1" / "probes" / "results" / job["job_id"]
                self.assertFalse(result.exists())

    def test_supervised_child_success_publishes_only_after_control_tree_removed(
        self,
    ) -> None:
        service = self._supervised_service("success")
        try:
            job = service.create_probe(self._request())
            service.execute_probe(job["job_id"])
            ready = service.get_probe(job["job_id"])
        finally:
            service.close()

        self.assertEqual("ready", ready["status"])
        result = self.repo_root / "data" / "ball_detector_development_v1" / "probes" / "results" / job["job_id"]
        self.assertTrue(result.is_dir())
        self.assertFalse((result / ".worker-control").exists())
        self.assertFalse((result / "test-worker-fixture.jpg").exists())

    def test_supervised_structured_failures_have_specific_recovery_and_no_publish(
        self,
    ) -> None:
        cases = (
            ("disk_exhausted", "free_disk_space"),
            ("device_out_of_memory", "reduce_probe_or_use_cpu"),
            ("corrupt_frame", "repair_review_source"),
        )
        for index, (code, recovery_action) in enumerate(cases, start=101):
            with self.subTest(code=code):
                service = self._supervised_service(f"structured-{code}")
                try:
                    job = service.create_probe(self._request(trial_intent_sha256=f"{index:064x}"))
                    service.execute_probe(job["job_id"])
                    failed = service.get_probe(job["job_id"])
                finally:
                    service.close()
                self.assertEqual("failed", failed["status"])
                self.assertEqual(code, failed["error_code"])
                self.assertEqual(recovery_action, failed["recovery_action"])
                self.assertIsNone(failed["report"])
                self.assertFalse(
                    (
                        self.repo_root / "data" / "ball_detector_development_v1" / "probes" / "results" / job["job_id"]
                    ).exists()
                )

    def test_supervised_worker_exit_codes_are_stable_and_publish_nothing(self) -> None:
        cases = (
            ("error-envelope-unavailable", "worker_error_envelope_unavailable"),
            ("disk-exit", "disk_exhausted"),
            ("unexpected-exit", "worker_died"),
        )
        for index, (mode, code) in enumerate(cases, start=111):
            with self.subTest(mode=mode):
                service = self._supervised_service(mode)
                try:
                    job = service.create_probe(self._request(trial_intent_sha256=f"{index:064x}"))
                    service.execute_probe(job["job_id"])
                    failed = service.get_probe(job["job_id"])
                finally:
                    service.close()
                self.assertEqual("failed", failed["status"])
                self.assertEqual(code, failed["error_code"])
                self.assertIsNone(failed["report"])

    def test_supervisor_deadline_and_heartbeat_timeout_reap_real_children(self) -> None:
        cases = (
            ("hang", 0.2, 2.0, "probe_worker_timeout"),
            ("silent-hang", 2.0, 0.2, "probe_worker_heartbeat_timeout"),
        )
        for index, (mode, deadline, heartbeat, code) in enumerate(cases, start=121):
            with self.subTest(mode=mode):
                service = self._supervised_service(
                    mode,
                    deadline=deadline,
                    heartbeat_timeout=heartbeat,
                )
                try:
                    job = service.create_probe(self._request(trial_intent_sha256=f"{index:064x}"))
                    service.execute_probe(job["job_id"])
                    failed = service.get_probe(job["job_id"])
                    self.assertEqual({}, service._probes()._children)
                finally:
                    service.close()
                self.assertEqual("failed", failed["status"])
                self.assertEqual(code, failed["error_code"])
                self.assertIsNone(failed["report"])

    def test_supervised_running_cancel_reaps_child_and_publishes_no_partial_result(
        self,
    ) -> None:
        service = self._supervised_service("cancel", auto_start=True)
        try:
            job = service.create_probe(self._request())
            self._wait_status(service, job["job_id"], {"running"})
            service.cancel_probe(job["job_id"])
            cancelled = self._wait_status(service, job["job_id"], {"cancelled"})
            self.assertEqual({}, service._probes()._children)
        finally:
            service.close()
        self.assertEqual("cancelled", cancelled["status"])
        self.assertIsNone(cancelled["report"])

    def test_supervised_close_reaps_child_and_requeues_without_forging_cancel(
        self,
    ) -> None:
        service = self._supervised_service("hang", auto_start=True)
        job = service.create_probe(self._request())
        self._wait_status(service, job["job_id"], {"running"})
        service.close()
        durable = json.loads(
            (
                self.repo_root / "data" / "ball_detector_development_v1" / "probes" / "jobs" / f"{job['job_id']}.json"
            ).read_text(encoding="utf-8")
        )

        observer = self._service()
        try:
            recovered = observer.get_probe(job["job_id"])
        finally:
            observer.close()
        self.assertEqual("queued", recovered["status"])
        self.assertEqual("recovered_after_shutdown", recovered["stage"])
        self.assertFalse(durable["cancel_requested"])
        self.assertIsNone(recovered["error_code"])
        self.assertIsNone(recovered["report"])

    def test_supervised_cancel_then_close_preserves_terminal_cancel(self) -> None:
        service = self._supervised_service("hang", auto_start=True)
        coordinator = service._probes()
        job = service.create_probe(self._request())
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            with coordinator._children_lock:
                if job["job_id"] in coordinator._children:
                    break
            time.sleep(0.025)
        else:
            service.close()
            self.fail("supervised child did not start")

        cancelling = service.cancel_probe(job["job_id"])
        self.assertEqual("running", cancelling["status"])
        service.close()
        with coordinator._children_lock:
            self.assertEqual({}, coordinator._children)

        observer = self._service()
        try:
            recovered = observer.get_probe(job["job_id"])
        finally:
            observer.close()
        self.assertEqual("cancelled", recovered["status"])
        self.assertEqual("cancelled", recovered["error_code"])
        self.assertIsNone(recovered["report"])

    def test_supervisor_terminates_descendant_when_worker_leader_exits(self) -> None:
        pid_path = self.repo_root / "detector-probe-descendant.pid"
        service = self._supervised_service("leader-with-descendant", descendant_pid_path=pid_path)
        try:
            job = service.create_probe(self._request())
            service.execute_probe(job["job_id"])
            failed = service.get_probe(job["job_id"])
        finally:
            service.close()
        descendant_pid = int(pid_path.read_text(encoding="ascii"))
        deadline = time.monotonic() + 5
        while _pid_exists(descendant_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(_pid_exists(descendant_pid))
        self.assertEqual("failed", failed["status"])
        self.assertEqual("worker_died", failed["error_code"])
        self.assertIsNone(failed["report"])

    def test_unconfirmed_worker_retains_staging_and_global_lease_until_exit(
        self,
    ) -> None:
        first = self._supervised_service("hang")
        first_coordinator = first._probes()
        with (
            patch.object(first_coordinator, "_signal_worker_tree", return_value=None),
            patch.object(first_coordinator, "_wait_for_worker_exit", return_value=False),
        ):
            first_job = first.create_probe(self._request())
            first.execute_probe(first_job["job_id"])
            first_failed = first.get_probe(first_job["job_id"])
            with first_coordinator._children_lock:
                child = first_coordinator._children[first_job["job_id"]]
            self.assertTrue(child.staging.is_dir())
            self.assertIsNotNone(first_coordinator._quarantined_execution_lease)

            second = self._supervised_service("success")
            errors: list[BaseException] = []
            second_job = second.create_probe(self._request(trial_intent_sha256=f"{131:064x}"))
            execution = threading.Thread(
                target=_capture_thread_error,
                args=(lambda: second.execute_probe(second_job["job_id"]), errors),
            )
            execution.start()
            time.sleep(0.25)
            self.assertTrue(execution.is_alive())
            self.assertEqual("queued", second.get_probe(second_job["job_id"])["status"])

            child.process.kill()
            execution.join(10)
            self.assertFalse(execution.is_alive())
            self.assertEqual([], errors)
            self.assertEqual("ready", second.get_probe(second_job["job_id"])["status"])
            second.close()

        deadline = time.monotonic() + 5
        while first_coordinator._children and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual({}, first_coordinator._children)
        self.assertFalse(child.staging.exists())
        self.assertEqual("worker_termination_failed", first_failed["error_code"])
        self.assertIsNone(first_failed["report"])
        first.close()

    def test_containment_false_or_exception_quarantines_before_cleanup(self) -> None:
        cases: tuple[object, ...] = (False, OSError("job query failed"))
        for index, first_result in enumerate(cases, start=141):
            with self.subTest(first_result=first_result):
                release_watcher = threading.Event()
                service = self._supervised_service("unexpected-exit")
                coordinator = service._probes()
                original = coordinator._close_worker_containment
                call_count = 0

                def flaky_close(child, *, terminate):
                    nonlocal call_count
                    call_count += 1
                    if call_count == 1:
                        if isinstance(first_result, BaseException):
                            raise first_result
                        return first_result
                    release_watcher.wait(5)
                    return original(child, terminate=terminate)

                with patch.object(
                    coordinator,
                    "_close_worker_containment",
                    side_effect=flaky_close,
                ):
                    job = service.create_probe(self._request(trial_intent_sha256=f"{index:064x}"))
                    service.execute_probe(job["job_id"])
                    failed = service.get_probe(job["job_id"])
                    self.assertTrue(coordinator._children)
                    self.assertIsNotNone(coordinator._quarantined_execution_lease)
                    release_watcher.set()
                    deadline = time.monotonic() + 5
                    while coordinator._children and time.monotonic() < deadline:
                        time.sleep(0.02)
                service.close()
                self.assertEqual({}, coordinator._children)
                self.assertEqual("worker_termination_failed", failed["error_code"])
                self.assertIsNone(failed["report"])

    def test_attach_failure_uses_abort_ack_or_quarantines_without_cleanup(self) -> None:
        service = self._supervised_service("unexpected-exit")
        coordinator = service._probes()
        with patch.object(
            coordinator,
            "_attach_worker_containment",
            side_effect=OSError("job assignment failed"),
        ):
            job = service.create_probe(self._request())
            service.execute_probe(job["job_id"])
            failed = service.get_probe(job["job_id"])
        self.assertEqual("worker_containment_unavailable", failed["error_code"])
        self.assertEqual({}, coordinator._children)
        service.close()

        quarantined = self._supervised_service("abort-hang")
        quarantined_coordinator = quarantined._probes()
        with (
            patch.object(
                quarantined_coordinator,
                "_attach_worker_containment",
                side_effect=OSError("job assignment failed"),
            ),
            patch.object(
                quarantined_coordinator,
                "_wait_for_worker_exit",
                return_value=False,
            ),
        ):
            job = quarantined.create_probe(self._request(trial_intent_sha256=f"{151:064x}"))
            quarantined.execute_probe(job["job_id"])
            failed = quarantined.get_probe(job["job_id"])
            with quarantined_coordinator._children_lock:
                child = quarantined_coordinator._children[job["job_id"]]
            deadline = time.monotonic() + 5
            while not (child.control / "abort-ack.json").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            acknowledgement = json.loads((child.control / "abort-ack.json").read_text(encoding="utf-8"))
            self.assertTrue(child.staging.exists())
            self.assertIsNotNone(quarantined_coordinator._quarantined_execution_lease)
            _terminate_pid(acknowledgement["worker_pid"])
            child.process.kill()
        deadline = time.monotonic() + 5
        while quarantined_coordinator._children and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual({}, quarantined_coordinator._children)
        self.assertFalse(child.staging.exists())
        self.assertEqual("worker_termination_failed", failed["error_code"])
        quarantined.close()

    def test_close_and_supervisor_finally_serialize_containment_cleanup(self) -> None:
        service = self._supervised_service("hang", auto_start=True)
        coordinator = service._probes()
        entered_signal = threading.Event()
        release_signal = threading.Event()
        original = coordinator._signal_worker_tree
        first_call = True

        def blocked_signal(child, *, force):
            nonlocal first_call
            if first_call:
                first_call = False
                entered_signal.set()
                release_signal.wait(5)
            return original(child, force=force)

        with patch.object(coordinator, "_signal_worker_tree", side_effect=blocked_signal):
            job = service.create_probe(self._request())
            self._wait_status(service, job["job_id"], {"running"})
            deadline = time.monotonic() + 5
            while not coordinator._children and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(coordinator._children)
            closer = threading.Thread(target=service.close)
            closer.start()
            self.assertTrue(entered_signal.wait(5))
            release_signal.set()
            closer.join(10)
            self.assertFalse(closer.is_alive())

        durable = json.loads((coordinator._jobs_root / f"{job['job_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual({}, coordinator._children)
        self.assertEqual("queued", durable["status"])
        self.assertEqual("recovered_after_shutdown", durable["stage"])
        self.assertFalse(durable["cancel_requested"])
        self.assertIsNone(durable["error_code"])

    def test_close_during_attach_waits_then_requeues_without_starting_inference(
        self,
    ) -> None:
        service = self._supervised_service("hang", auto_start=True)
        coordinator = service._probes()
        attach_entered = threading.Event()
        release_attach = threading.Event()
        original = coordinator._attach_worker_containment

        def blocked_attach(child):
            attach_entered.set()
            release_attach.wait(5)
            return original(child)

        with patch.object(coordinator, "_attach_worker_containment", side_effect=blocked_attach):
            job = service.create_probe(self._request())
            self.assertTrue(attach_entered.wait(5))
            closer = threading.Thread(target=service.close)
            closer.start()
            time.sleep(0.1)
            self.assertTrue(closer.is_alive())
            release_attach.set()
            closer.join(10)
            self.assertFalse(closer.is_alive())

        durable = json.loads((coordinator._jobs_root / f"{job['job_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual({}, coordinator._children)
        self.assertFalse(coordinator._execution_quarantined)
        self.assertIsNone(coordinator._quarantined_execution_lease)
        self.assertEqual("queued", durable["status"])
        self.assertEqual("recovered_after_shutdown", durable["stage"])
        self.assertFalse(durable["cancel_requested"])

    def test_close_during_attach_failure_waits_for_local_abort_ack(self) -> None:
        service = self._supervised_service("hang", auto_start=True)
        coordinator = service._probes()
        abort_entered = threading.Event()
        release_abort = threading.Event()
        original_abort = coordinator._abort_uncontained_worker

        def blocked_abort(child, control, worker_id):
            abort_entered.set()
            release_abort.wait(5)
            return original_abort(child, control, worker_id)

        with (
            patch.object(
                coordinator,
                "_attach_worker_containment",
                side_effect=OSError("job assignment failed"),
            ),
            patch.object(
                coordinator,
                "_abort_uncontained_worker",
                side_effect=blocked_abort,
            ),
        ):
            job = service.create_probe(self._request())
            self.assertTrue(abort_entered.wait(5))
            self.assertEqual({}, coordinator._children)
            closer = threading.Thread(target=service.close)
            closer.start()
            time.sleep(0.1)
            self.assertTrue(closer.is_alive())
            self.assertEqual({}, coordinator._children)
            release_abort.set()
            closer.join(10)
            self.assertFalse(closer.is_alive())

        durable = json.loads((coordinator._jobs_root / f"{job['job_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual({}, coordinator._children)
        self.assertFalse(coordinator._execution_quarantined)
        self.assertIsNone(coordinator._quarantined_execution_lease)
        self.assertEqual("queued", durable["status"])
        self.assertEqual("recovered_after_shutdown", durable["stage"])

    def test_parent_crash_kills_worker_and_descendant_process_tree(self) -> None:
        state = self.repo_root / "parent-crash-containment"
        shutil.rmtree(state, ignore_errors=True)
        state.mkdir()
        fixture_root = Path(__file__).parent / "fixtures"
        harness = fixture_root / "detector_probe_parent_crash_harness.py"
        worker = fixture_root / "detector_probe_test_worker.py"
        process = subprocess.Popen(
            [sys.executable, str(harness), str(state), str(worker)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            ready_path = state / "ready.json"
            deadline = time.monotonic() + 10
            while not ready_path.exists() and time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(f"parent-crash harness exited early ({process.returncode})")
                time.sleep(0.02)
            self.assertTrue(ready_path.exists())
            ready = json.loads(ready_path.read_text(encoding="utf-8"))
            _terminate_pid(ready["harness_pid"])
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and (
                _pid_exists(ready["worker_pid"]) or _pid_exists(ready["descendant_pid"])
            ):
                time.sleep(0.02)
            self.assertFalse(_pid_exists(ready["worker_pid"]))
            self.assertFalse(_pid_exists(ready["descendant_pid"]))
            self.assertFalse((state / "staging" / "detector_probe_report.v1.json").exists())
            self.assertFalse((state / "staging" / "detector_probe_manifest.v1.json").exists())
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def test_real_heartbeat_enospc_uses_stable_disk_exhausted_exit(self) -> None:
        control = self.repo_root / "heartbeat-enospc-control"
        control.mkdir(exist_ok=True)
        harness = Path(__file__).parent / "fixtures" / "detector_probe_heartbeat_enospc_harness.py"
        completed = subprocess.run(
            [sys.executable, str(harness), str(control)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        self.assertEqual(77, completed.returncode)

    def test_corrupt_or_wrong_dimension_visual_evidence_never_publishes_ready(self) -> None:
        cases = [
            (b"not-a-jpeg", "corrupt_frame"),
            (_jpeg_fixture(640, 360), "artifact_dimension_mismatch"),
        ]
        for index, (payload, expected_code) in enumerate(cases, start=1):
            with self.subTest(expected_code=expected_code):

                def runner(request, profiles, staging, should_cancel, progress, payload=payload):
                    output = self._successful_runner(request, profiles, staging, should_cancel, progress)
                    first = output["frames"][0]
                    (staging / first["source_frame_relative_path"]).write_bytes(payload)
                    return output

                service = self._service(runner)
                try:
                    job = service.create_probe(self._request(trial_intent_sha256=f"{index + 20:064x}"))
                    service.execute_probe(job["job_id"])
                    failed = service.get_probe(job["job_id"])
                finally:
                    service.close()
                self.assertEqual("failed", failed["status"])
                self.assertEqual(expected_code, failed["error_code"])
                result = self.repo_root / "data" / "ball_detector_development_v1" / "probes" / "results" / job["job_id"]
                self.assertFalse(result.exists())

    def test_partial_or_unlisted_runner_output_never_publishes_ready(self) -> None:
        def remove_frame(output, _staging):
            output["frames"].pop()

        def remove_profile(output, _staging):
            output["frames"][0]["profile_results"].pop()

        def add_unlisted(_output, staging):
            unlisted = staging / "overlays" / "unlisted.jpg"
            unlisted.parent.mkdir(parents=True, exist_ok=True)
            unlisted.write_bytes(_jpeg_fixture())

        cases = [
            (remove_frame, "partial_probe_result"),
            (remove_profile, "partial_probe_result"),
            (add_unlisted, "artifact_allowlist_mismatch"),
        ]
        for index, (mutate, expected_code) in enumerate(cases, start=31):
            with self.subTest(expected_code=expected_code):

                def runner(request, profiles, staging, should_cancel, progress, mutate=mutate):
                    output = self._successful_runner(request, profiles, staging, should_cancel, progress)
                    mutate(output, staging)
                    return output

                service = self._service(runner)
                try:
                    job = service.create_probe(self._request(trial_intent_sha256=f"{index:064x}"))
                    service.execute_probe(job["job_id"])
                    failed = service.get_probe(job["job_id"])
                finally:
                    service.close()
                self.assertEqual("failed", failed["status"])
                self.assertEqual(expected_code, failed["error_code"])
                result = self.repo_root / "data" / "ball_detector_development_v1" / "probes" / "results" / job["job_id"]
                self.assertFalse(result.exists())

    def test_linked_visual_evidence_is_rejected_and_cleanup_never_touches_target(self) -> None:
        external = self.repo_root / "external-probe-evidence.jpg"
        external.write_bytes(_jpeg_fixture())
        preflight = self.repo_root / "probe-symlink-preflight.jpg"
        try:
            os.symlink(external, preflight)
        except OSError as exc:
            self.skipTest(f"file symlinks are unavailable: {exc}")
        else:
            preflight.unlink()

        def runner(request, profiles, staging, should_cancel, progress):
            output = self._successful_runner(request, profiles, staging, should_cancel, progress)
            source = staging / output["frames"][0]["source_frame_relative_path"]
            source.unlink()
            os.symlink(external, source)
            return output

        service = self._service(runner)
        try:
            job = service.create_probe(self._request())
            service.execute_probe(job["job_id"])
            failed = service.get_probe(job["job_id"])
        finally:
            service.close()
        self.assertEqual("failed", failed["status"])
        self.assertEqual("unsafe_path", failed["error_code"])
        self.assertEqual(_jpeg_fixture(), external.read_bytes())
        results = self.repo_root / "data" / "ball_detector_development_v1" / "probes" / "results"
        self.assertFalse((results / job["job_id"]).exists())
        self.assertEqual([], list(results.glob(f".{job['job_id']}.staging-*")))

    def test_source_change_after_freeze_blocks_execution_without_report(self) -> None:
        original = self.source.read_bytes()
        service = self._service()
        try:
            job = service.create_probe(self._request())
            self.source.write_bytes(b"changed after freeze")
            service.execute_probe(job["job_id"])
            blocked = service.get_probe(job["job_id"])
        finally:
            self.source.write_bytes(original)
            service.close()
        self.assertEqual("blocked", blocked["status"])
        self.assertEqual("source_changed", blocked["blocker_code"])
        self.assertIsNone(blocked["report"])

    def test_persisted_immutable_request_digest_tamper_is_rejected(self) -> None:
        first = self._service()
        job = first.create_probe(self._request())
        first.close()
        job_file = (
            self.repo_root / "data" / "ball_detector_development_v1" / "probes" / "jobs" / f"{job['job_id']}.json"
        )
        record = json.loads(job_file.read_text(encoding="utf-8"))
        record["frozen_request"]["source_sha256"] = SHA_C
        _atomic_json(job_file, record)

        second = self._service()
        try:
            with self.assertRaisesRegex(DetectorDevelopmentError, "Persisted"):
                second.get_probe(job["job_id"])
        finally:
            second.close()

    def test_legacy_persisted_job_without_execution_bundle_is_rejected(self) -> None:
        first = self._service()
        job = first.create_probe(self._request())
        first.close()
        job_file = (
            self.repo_root / "data" / "ball_detector_development_v1" / "probes" / "jobs" / f"{job['job_id']}.json"
        )
        record = json.loads(job_file.read_text(encoding="utf-8"))
        record["frozen_request"].pop("execution_bundle")
        record["frozen_request"].pop("execution_bundle_sha256")
        record["frozen_request"].pop("runtime_environment_sha256")
        record["frozen_request"].pop("frozen_profiles_sha256")
        _atomic_json(job_file, record)

        second = self._service()
        try:
            with self.assertRaisesRegex(DetectorDevelopmentError, "Persisted"):
                second.get_probe(job["job_id"])
        finally:
            second.close()

    def test_tampered_ready_artifact_is_blocked_after_restart(self) -> None:
        first = self._service()
        job = first.create_probe(self._request())
        first.execute_probe(job["job_id"])
        first.close()
        artifact = (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "probes"
            / "results"
            / job["job_id"]
            / "frames"
            / "000000003.jpg"
        )
        artifact.write_bytes(b"tampered")

        second = self._service()
        try:
            blocked = second.get_probe(job["job_id"])
        finally:
            second.close()
        self.assertEqual("blocked", blocked["status"])
        self.assertEqual("persisted_result_invalid", blocked["blocker_code"])
        self.assertIsNone(blocked["report"])

    def test_restart_recovery_requeues_noncommitting_job_but_does_not_invent_partial_success(self) -> None:
        first = self._service()
        job = first.create_probe(self._request())
        job_file = (
            self.repo_root / "data" / "ball_detector_development_v1" / "probes" / "jobs" / f"{job['job_id']}.json"
        )
        record = json.loads(job_file.read_text(encoding="utf-8"))
        record.update({"status": "running", "stage": "inference", "owner_id": "dead-owner"})
        _atomic_json(job_file, record)
        first.close()

        second = self._service()
        try:
            recovered = second.get_probe(job["job_id"])
            self.assertEqual("queued", recovered["status"])
            self.assertEqual("recovered_after_restart", recovered["stage"])
            second.execute_probe(job["job_id"])
            self.assertEqual("ready", second.get_probe(job["job_id"])["status"])
        finally:
            second.close()

    def test_restart_recovery_uses_complete_commit_but_rejects_missing_commit(self) -> None:
        first = self._service()
        completed = first.create_probe(self._request())
        first.execute_probe(completed["job_id"])
        missing = first.create_probe(self._request(trial_intent_sha256=SHA_A))
        first.close()
        jobs_root = self.repo_root / "data" / "ball_detector_development_v1" / "probes" / "jobs"
        for job, progress in ((completed, "complete"), (missing, "missing")):
            path = jobs_root / f"{job['job_id']}.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            record.update(
                {
                    "status": "committing",
                    "stage": "committing",
                    "owner_id": f"dead-{progress}",
                }
            )
            _atomic_json(path, record)

        second = self._service()
        try:
            recovered_complete = second.get_probe(completed["job_id"])
            recovered_missing = second.get_probe(missing["job_id"])
        finally:
            second.close()
        self.assertEqual("ready", recovered_complete["status"])
        self.assertIsNotNone(recovered_complete["report"])
        self.assertEqual("failed", recovered_missing["status"])
        self.assertEqual("commit_interrupted", recovered_missing["error_code"])
        self.assertIsNone(recovered_missing["report"])

    @staticmethod
    def _wait_terminal(service: DetectorDevelopmentService, job_id: str) -> dict[str, object]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = service.get_probe(job_id)
            if status["status"] in {"ready", "failed", "cancelled", "blocked"}:
                return status
            time.sleep(0.01)
        raise AssertionError("probe did not reach a terminal state")


if __name__ == "__main__":
    unittest.main()
