from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import cv2
import numpy as np
import yaml
from fastapi.testclient import TestClient

from football_tracking.api.app import create_app
from football_tracking.api.dependencies import get_service
from football_tracking.api.schemas import (
    DetectorProbeExecutionEnvironmentView,
    DetectorProbeJobResponse,
)
from football_tracking.api.service import ApiService
from football_tracking.detector_development import DetectorDevelopmentService
from football_tracking.detector_development_common import (
    DetectorDevelopmentError,
    canonical_sha256,
)
from football_tracking.detector_model_registry import build_builtin_model_catalog


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DetectorApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temporary.name)
        for name in ("config/generated", "data", "outputs/runs/source", "weights"):
            (self.repo_root / name).mkdir(parents=True, exist_ok=True)
        self.source = self.repo_root / "data" / "source.mp4"
        self.source.write_bytes(b"source-bound-detector-probe-fixture")
        self.source_sha256 = _sha256(self.source)
        (self.repo_root / "weights" / "fixture.pt").write_bytes(b"fixture")
        self.parent_trial_id = "production_trial_trial-1"
        self.output_dir = self.repo_root / "outputs" / "runs" / "source" / self.parent_trial_id
        self.output_dir.mkdir()
        self.note = {
            "schema_version": "1.0",
            "purpose": "production_trial",
            "workflow_id": "workflow-1",
            "submission_id": "submission-1",
            "output_id": "trial-1",
            "generation": 1,
            "start_frame": 100,
            "max_frames": 10,
            "enable_postprocess": True,
            "enable_follow_cam": True,
            "calibration_digest": "a" * 64,
            "intent_sha256": "b" * 64,
        }
        self.base_config_path = self.repo_root / "config" / "base.yaml"
        self.base_config_path.write_text("detector:\n  confidence_threshold: 0.2\n", encoding="utf-8")
        self.base_config_sha256 = _sha256(self.base_config_path)
        source_stat = self.source.stat()
        self.config_path = self.repo_root / "config" / "generated" / "trial.yaml"
        self.config_path.write_text(
            yaml.safe_dump(
                {
                    "input_video": str(self.source.resolve()),
                    "metadata": {
                        "production_workflow": {
                            **self.note,
                            "base_config_lineage": {
                                "name": "base.yaml",
                                "sha256": self.base_config_sha256,
                            },
                            "source_signature": {
                                "path": str(self.source.resolve()),
                                "size_bytes": source_stat.st_size,
                                "modified_at": datetime.fromtimestamp(
                                    source_stat.st_mtime, tz=timezone.utc
                                ).isoformat(),
                            },
                        }
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self.config_sha256 = _sha256(self.config_path)
        self.contract_path = self.output_dir / "tracking_contract.v2.json"
        self._write_contract(self.source_sha256)
        self.service = ApiService(self.repo_root)
        self._write_parent_run()
        self.catalog = self._probe_catalog()
        self.development = DetectorDevelopmentService(
            self.repo_root,
            probe_runner=self._successful_runner,
            auto_start_workers=False,
            catalog_provider=lambda: deepcopy(self.catalog),
        )
        self.service._detector_development = self.development
        app = create_app(self.repo_root, initialize_service=False)
        app.dependency_overrides[get_service] = lambda: self.service
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.service.close()
        self.temporary.cleanup()

    def test_unknown_cross_origin_is_not_authorized_for_probe_or_artifact_access(self) -> None:
        before = list((self.repo_root / "data" / "ball_detector_development_v1" / "probes" / "jobs").glob("*.json"))
        preflight = self.client.options(
            "/api/v1/detector-probes",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        catalog = self.client.get(
            "/api/v1/detector-models",
            headers={"Origin": "https://evil.example"},
        )
        rejected_create = self.client.post(
            "/api/v1/detector-probes",
            headers={"Origin": "https://evil.example"},
            json={
                "parent_trial_id": self.parent_trial_id,
                "profile_ids": [profile["profile_id"] for profile in self.catalog["profiles"]],
                "top_k": 5,
            },
        )

        self.assertNotIn("access-control-allow-origin", preflight.headers)
        self.assertNotIn("access-control-allow-origin", catalog.headers)
        self.assertEqual(403, rejected_create.status_code)
        self.assertEqual(
            before, list((self.repo_root / "data" / "ball_detector_development_v1" / "probes" / "jobs").glob("*.json"))
        )
        self.assertEqual(
            200,
            self.client.get(
                "/api/v1/detector-models",
                headers={"Origin": "http://127.0.0.1:5173"},
            ).status_code,
        )
        self.assertEqual(200, self.client.get("/api/v1/detector-models").status_code)

    def test_cpu_execution_allows_cuda_built_torch_without_gpu_identity(self) -> None:
        decoder_fingerprint = {
            "python_implementation": "CPython",
            "python_version": "3.11.9",
            "numpy_version": "2.1.0",
            "opencv_version": "4.10.0",
            "opencv_build_information_sha256": "e" * 64,
            "opencv_ffmpeg_enabled": True,
        }
        environment = DetectorProbeExecutionEnvironmentView.model_validate(
            {
                "device": "cpu",
                "precision": "fp32",
                "cuda_available": False,
                "cuda_device_count": 0,
                "cuda_visible_devices": "-1",
                "cuda_compiled_version": "12.6",
                "cudnn_version": 90501,
                "gpu_name": None,
                "gpu_compute_capability": None,
                "gpu_total_memory_bytes": None,
                "cuda_driver_version": None,
                "pydantic_version": "2.11.7",
                "pydantic_core_version": "2.33.2",
                **decoder_fingerprint,
                "decoder_fingerprint_sha256": canonical_sha256(decoder_fingerprint),
            }
        )

        self.assertEqual("cpu", environment.device)
        self.assertEqual("12.6", environment.cuda_compiled_version)

    def test_request_validation_never_echoes_or_fails_encoding_surrogates(self) -> None:
        valid_profiles = [profile["profile_id"] for profile in self.catalog["profiles"]]
        requests = [
            (
                "/api/v1/detector-probes",
                ('{"parent_trial_id":"bad-\\ud800","profile_ids":' + json.dumps(valid_profiles) + ',"top_k":5}').encode(
                    "ascii"
                ),
            ),
            (
                "/api/v1/detector-models/import",
                ('{"package_relative_path":"' + "\\ud800" * 501 + '","manifest_sha256":"' + "a" * 64 + '"}').encode(
                    "ascii"
                ),
            ),
            (
                "/api/v1/detector-probes",
                b'{"parent_trial_id":"\xff","profile_ids":[],"top_k":5}',
            ),
        ]
        for path, body in requests:
            with self.subTest(path=path, body_prefix=body[:40]):
                response = self.client.post(
                    path,
                    content=body,
                    headers={"Content-Type": "application/json"},
                )
                self.assertIn(response.status_code, {400, 422})
                response.content.decode("utf-8", errors="strict")
                payload = response.json()
                self.assertIn("detail", payload)
                self.assertNotIn('"input"', response.text)

    def _write_contract(self, source_sha256: str) -> None:
        self.contract_path.write_text(
            json.dumps(
                {
                    "schema_version": "2.0",
                    "generated_at": "2026-07-17T12:00:00+00:00",
                    "source": {
                        "video_sha256": source_sha256,
                        "width": 64,
                        "height": 32,
                        "frame_count": 200,
                        "fps": 20.0,
                    },
                    "summary": {
                        "status": "ok",
                        "frame_count": 10,
                        "candidate_count": 0,
                        "classification_count": 0,
                        "decision_count": 0,
                        "prelabel_count": 0,
                        "confirmed_label_count": 0,
                        "validation_error_count": 0,
                    },
                    "frames": [
                        {
                            "frame_index": frame_index,
                            "status": "unknown",
                            "confidence": 0.0,
                            "source": "tracking_pipeline",
                            "reason": "no_filtered_candidates",
                            "legacy_status": "Lost",
                        }
                        for frame_index in range(100, 110)
                    ],
                    "candidates": [],
                    "classifications": [],
                    "decisions": [],
                    "validation_errors": [],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def _write_parent_run(self) -> None:
        registry = self.service._read_registry()
        registry["runs"] = [
            {
                "run_id": self.parent_trial_id,
                "source": "api",
                "status": "completed",
                "created_at": "2026-07-17T12:00:00+00:00",
                "started_at": "2026-07-17T12:00:00+00:00",
                "completed_at": "2026-07-17T12:01:00+00:00",
                "config_name": "generated/trial.yaml",
                "config_path": str(self.config_path.resolve()),
                "config_sha256": self.config_sha256,
                "input_video": str(self.source.resolve()),
                "parent_run_id": None,
                "output_dir": str(self.output_dir.resolve()),
                "modules_enabled": {
                    "postprocess": True,
                    "follow_cam": True,
                    "temporal_chunks": False,
                    "broadcast_hybrid": False,
                },
                "artifacts": [],
                "stats": {},
                "progress": {
                    "stage": "completed",
                    "current_frame": 10,
                    "total_frames": 10,
                    "percent": 100.0,
                },
                "notes": json.dumps(self.note, sort_keys=True),
                "error": None,
            }
        ]
        self.service._write_registry(registry)

    def _probe_catalog(self) -> dict[str, object]:
        source_root = Path(__file__).resolve().parents[1]
        catalog = build_builtin_model_catalog(source_root)
        model = deepcopy(
            next(item for item in catalog["models"] if item["descriptor"]["model_id"] == "official-coco-yolo11n")
        )
        model["descriptor"]["weights"] = {
            "relative_path": "weights/fixture.pt",
            "sha256": _sha256(self.repo_root / "weights" / "fixture.pt"),
            "size_bytes": 7,
        }
        model["descriptor"]["descriptor_sha256"] = "c" * 64
        model["availability"]["status"] = "available"
        model["availability"]["observations"]["runtime_load"] = {
            "status": "pass",
            "reason": "fixture_runtime_evidence",
            "installed_runtime": {
                "ultralytics": "8.4.31",
                "sahi": "0.11.36",
                "torch": "2.7.1+cpu",
            },
            "evidence_sha256": "d" * 64,
        }
        model["selectable_for_probe"] = True
        profiles = []
        for profile in catalog["profiles"]:
            if profile["model_id"] != "official-coco-yolo11n":
                continue
            copied = deepcopy(profile)
            copied["model_descriptor_sha256"] = "c" * 64
            copied["availability"]["status"] = "available"
            copied["selectable_for_probe"] = True
            profiles.append(copied)
        return {
            "schema_version": "1.0",
            "artifact_type": "ball_detector_development_v1",
            "models": [model],
            "profiles": profiles,
            "catalog_findings": [],
        }

    @staticmethod
    def _jpeg() -> bytes:
        image = np.zeros((32, 64, 3), dtype=np.uint8)
        image[:, :] = (16, 128, 16)
        cv2.line(image, (0, 16), (63, 16), (240, 240, 240), 1)
        encoded, payload = cv2.imencode(".jpg", image)
        if not encoded:
            raise AssertionError("JPEG fixture could not be encoded")
        return payload.tobytes()

    @classmethod
    def _successful_runner(cls, request, profiles, staging, should_cancel, progress):
        jpeg = cls._jpeg()
        frames = []
        completed = 0
        for frame_index in request["frame_indices"]:
            source_path = staging / "frames" / f"{frame_index:09d}.jpg"
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(jpeg)
            profile_results = []
            for profile in profiles:
                if should_cancel():
                    raise AssertionError("unexpected cancellation")
                profile_id = profile["profile_id"]
                overlay_path = staging / "overlays" / f"{frame_index:09d}-{profile_id}.jpg"
                overlay_path.parent.mkdir(parents=True, exist_ok=True)
                overlay_path.write_bytes(jpeg)
                candidate = {
                    "frame_index": frame_index,
                    "bbox_source_px": [10.0, 8.0, 18.0, 16.0],
                    "confidence": 0.8,
                    "class_name": "ball",
                    "checkpoint_class_name": "sports ball",
                    "source": f"yolo_{profile['mode']}",
                    "coordinate_reason": (
                        "direct_source_coordinates" if profile["mode"] == "direct" else "sahi_tile_offset_applied"
                    ),
                    "merge_reason": "retained_top_k",
                }
                profile_results.append(
                    {
                        "profile_id": profile_id,
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
                completed += 1
                progress(completed, len(request["frame_indices"]) * len(profiles))
            frames.append(
                {
                    "frame_index": frame_index,
                    "source_frame_relative_path": source_path.relative_to(staging).as_posix(),
                    "source_width": 64,
                    "source_height": 32,
                    "requested_decode_mode": "preroll",
                    "effective_decode_mode": "preroll_verified",
                    "decoded_frame_position": frame_index,
                    "media_integrity": {
                        "path": None,
                        "status": "ok",
                        "width": 64,
                        "height": 32,
                        "mean_luma": 80.0,
                        "std_luma": 10.0,
                        "texture_tile_ratio": 0.5,
                        "dominant_color_ratio": 0.5,
                        "gray": False,
                        "low_information": False,
                        "likely_corrupt": False,
                        "reasons": [],
                    },
                    "profile_results": profile_results,
                }
            )
        return {
            "frames": frames,
            "decode": {
                "width": 64,
                "height": 32,
                "frame_count": 200,
                "fps": 20.0,
                "requested_decode_mode": "preroll",
                "effective_decode_mode": "preroll_verified",
                "verified_frame_indices": list(request["frame_indices"]),
                "position_verification": "opencv_next_frame_index_with_0.25_tolerance",
            },
            "execution": {
                "device": request["_execution_environment"]["device"],
                "precision": request["_execution_environment"]["precision"],
            },
        }

    def _request(self, **patch: object) -> dict[str, object]:
        request: dict[str, object] = {
            "parent_trial_id": self.parent_trial_id,
            "profile_ids": [
                "official-coco-yolo11n-direct",
                "official-coco-yolo11n-sahi",
            ],
            "frame_indices": [100, 105, 109],
            "top_k": 5,
        }
        request.update(patch)
        return request

    def test_public_request_forbids_all_client_authority_fields(self) -> None:
        forged_fields = {
            "source_relative_path": "data/other.mp4",
            "source_sha256": "f" * 64,
            "tracking_contract_relative_path": "outputs/forged.json",
            "tracking_contract_sha256": "f" * 64,
            "base_config_sha256": "f" * 64,
            "tuning_patch_sha256": "f" * 64,
            "requested_decode_mode": "direct",
            "model_path": "weights/forged.pt",
        }
        for name, value in forged_fields.items():
            with self.subTest(name=name):
                response = self.client.post("/api/v1/detector-probes", json={**self._request(), name: value})
                self.assertEqual(422, response.status_code)

    def test_parent_status_and_purpose_are_authoritative(self) -> None:
        valid_parent = self.service.get_run(self.parent_trial_id)
        for patch, expected_code in (
            ({"status": "running"}, "parent_trial_not_completed"),
            (
                {"notes": json.dumps({**self.note, "purpose": "production_full"}, sort_keys=True)},
                "invalid_parent_trial_purpose",
            ),
        ):
            with (
                self.subTest(expected_code=expected_code),
                mock.patch.object(self.service, "get_run", return_value={**valid_parent, **patch}),
            ):
                response = self.client.post("/api/v1/detector-probes", json=self._request())
                self.assertIn(response.status_code, {400, 409})
                self.assertEqual(expected_code, response.json()["detail"]["code"])

    def test_contract_path_hash_source_and_config_mismatches_fail_closed(self) -> None:
        original_contract = self.contract_path.read_bytes()
        original_config = self.config_path.read_bytes()
        try:
            self.contract_path.unlink()
            missing = self.client.post("/api/v1/detector-probes", json=self._request())
            self.assertEqual(409, missing.status_code)
            self.assertEqual("parent_tracking_contract_unavailable", missing.json()["detail"]["code"])

            self.contract_path.write_bytes(original_contract)
            self._write_contract("f" * 64)
            wrong_source = self.client.post("/api/v1/detector-probes", json=self._request())
            self.assertEqual(202, wrong_source.status_code, wrong_source.text)
            created = wrong_source.json()
            self.development.execute_probe(created["job_id"])
            blocked_response = self.client.get(created["status_url"])
            self.assertEqual(200, blocked_response.status_code, blocked_response.text)
            blocked = blocked_response.json()
            self.assertEqual("blocked", blocked["status"])
            self.assertEqual("source_digest_mismatch", blocked["blocker_code"])
            self.assertEqual("refresh_lineage", blocked["recovery_action"])

            self.contract_path.write_bytes(original_contract)
            self.config_path.write_bytes(original_config + b"\n# changed\n")
            wrong_config = self.client.post("/api/v1/detector-probes", json=self._request())
            self.assertEqual(409, wrong_config.status_code)
            self.assertEqual("parent_config_digest_mismatch", wrong_config.json()["detail"]["code"])
        finally:
            self.contract_path.write_bytes(original_contract)
            self.config_path.write_bytes(original_config)

    def test_parent_contract_must_exactly_match_the_trial_frame_window(self) -> None:
        original = json.loads(self.contract_path.read_text(encoding="utf-8"))
        cases = {
            "same-source-other-run": {
                **original,
                "frames": [{**item, "frame_index": index} for index, item in enumerate(original["frames"])],
            },
            "missing": {**original, "frames": original["frames"][:-1]},
            "duplicate": {
                **original,
                "frames": [*original["frames"][:-1], original["frames"][0]],
            },
            "non-contiguous": {
                **original,
                "frames": [
                    *original["frames"][:-1],
                    {**original["frames"][-1], "frame_index": 110},
                ],
            },
            "summary-mismatch": {
                **original,
                "summary": {**original["summary"], "frame_count": 9},
            },
        }
        try:
            for name, payload in cases.items():
                with self.subTest(name=name):
                    self.contract_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
                    response = self.client.post("/api/v1/detector-probes", json=self._request())
                    self.assertEqual(409, response.status_code)
                    self.assertEqual(
                        "invalid_parent_tracking_contract",
                        response.json()["detail"]["code"],
                    )
        finally:
            self.contract_path.write_text(json.dumps(original, sort_keys=True), encoding="utf-8")

    def test_create_get_ready_artifact_and_full_lineage_schema_round_trip(self) -> None:
        response = self.client.post("/api/v1/detector-probes", json=self._request())
        self.assertEqual(202, response.status_code, response.text)
        created = response.json()
        self.assertEqual(
            {
                "job_id",
                "request_sha256",
                "status",
                "status_url",
                "cancel_url",
                "retry_from_job_id",
            },
            set(created),
        )
        self.assertEqual("queued", created["status"])

        duplicate = self.client.post("/api/v1/detector-probes", json=self._request())
        self.assertEqual(created["job_id"], duplicate.json()["job_id"])
        self.development.execute_probe(created["job_id"])

        ready_response = self.client.get(created["status_url"])
        self.assertEqual(200, ready_response.status_code, ready_response.text)
        ready = ready_response.json()
        round_trip = DetectorProbeJobResponse.model_validate(ready).model_dump(mode="json")
        DetectorProbeJobResponse.model_validate(round_trip)

        self.assertEqual("ready", round_trip["status"])
        self.assertEqual(self.source_sha256, round_trip["frozen_request"]["source_sha256"])
        self.assertEqual(self.base_config_sha256, round_trip["frozen_request"]["base_config_sha256"])
        self.assertEqual(self.config_sha256, round_trip["frozen_request"]["effective_config_sha256"])
        self.assertEqual(self.note["intent_sha256"], round_trip["frozen_request"]["trial_intent_sha256"])
        self.assertEqual("absent", round_trip["frozen_request"]["tuning_patch_binding"]["state"])
        self.assertEqual(
            {
                round_trip["frozen_request"]["base_config_sha256"],
                round_trip["frozen_request"]["effective_config_sha256"],
                round_trip["frozen_request"]["trial_intent_sha256"],
                round_trip["frozen_request"]["tuning_patch_sha256"],
            },
            {
                self.base_config_sha256,
                self.config_sha256,
                self.note["intent_sha256"],
                round_trip["frozen_request"]["tuning_patch_sha256"],
            },
        )
        self.assertEqual(
            4,
            len(
                {
                    round_trip["frozen_request"]["base_config_sha256"],
                    round_trip["frozen_request"]["effective_config_sha256"],
                    round_trip["frozen_request"]["trial_intent_sha256"],
                    round_trip["frozen_request"]["tuning_patch_sha256"],
                }
            ),
        )
        self.assertEqual(
            _sha256(self.contract_path),
            round_trip["frozen_request"]["tracking_contract_sha256"],
        )
        self.assertEqual(
            set(round_trip["frozen_request"]["profile_ids"]),
            set(round_trip["frozen_request"]["profile_sha256s"]),
        )
        self.assertEqual(
            round_trip["frozen_request"]["profile_sha256s"],
            round_trip["report"]["lineage"]["profile_sha256s"],
        )
        self.assertEqual(
            round_trip["frozen_request"]["execution_bundle"],
            round_trip["report"]["lineage"]["execution_bundle"],
        )
        self.assertEqual(
            round_trip["frozen_request"]["execution_bundle_sha256"],
            round_trip["report"]["lineage"]["execution_bundle_sha256"],
        )
        self.assertEqual(
            round_trip["frozen_request"]["runtime_environment_sha256"],
            round_trip["report"]["lineage"]["runtime_environment_sha256"],
        )
        self.assertEqual(
            round_trip["frozen_request"]["execution_bundle"]["execution_environment"]["device"],
            round_trip["report"]["execution"]["device"],
        )
        self.assertRegex(
            round_trip["frozen_request"]["execution_bundle"]["execution_environment"]["decoder_fingerprint_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual("fp32", round_trip["report"]["execution"]["precision"])
        self.assertEqual([100, 105, 109], round_trip["report"]["decode"]["verified_frame_indices"])
        self.assertRegex(round_trip["result_manifest_sha256"], r"^[0-9a-f]{64}$")
        candidate = round_trip["report"]["frames"][0]["profile_results"][0]["raw_candidates"][0]
        self.assertEqual(100, candidate["frame_index"])
        self.assertEqual("direct_source_coordinates", candidate["coordinate_reason"])
        self.assertEqual("sports ball", candidate["checkpoint_class_name"])

        source_url = round_trip["report"]["frames"][0]["source_artifact_url"]
        artifact_response = self.client.get(source_url)
        self.assertEqual(200, artifact_response.status_code)
        self.assertEqual("no-store", artifact_response.headers["cache-control"])
        self.assertEqual(
            round_trip["report"]["frames"][0]["source_frame_sha256"],
            artifact_response.headers["x-content-sha256"],
        )
        self.assertEqual(self._jpeg(), artifact_response.content)

    def test_artifact_reader_capacity_exhaustion_is_a_stable_503(self) -> None:
        response = self.client.post("/api/v1/detector-probes", json=self._request())
        self.assertEqual(202, response.status_code, response.text)
        created = response.json()
        self.development.execute_probe(created["job_id"])
        ready = self.client.get(created["status_url"]).json()
        source_url = ready["report"]["frames"][0]["source_artifact_url"]
        coordinator = self.development._probes()
        original_slots = coordinator._artifact_read_slots
        coordinator._artifact_read_slots = mock.Mock(
            acquire=mock.Mock(return_value=False),
            release=mock.Mock(),
        )
        try:
            artifact_response = self.client.get(source_url)
        finally:
            coordinator._artifact_read_slots = original_slots

        self.assertEqual(503, artifact_response.status_code)
        self.assertEqual(
            "artifact_read_capacity_exceeded",
            artifact_response.json()["detail"]["code"],
        )

    def test_failed_and_blocked_jobs_hide_internal_error_messages(self) -> None:
        coordinator = self.development._probes()

        def failed_runner(*_args, **_kwargs):
            raise RuntimeError(r"internal failure at C:\private\weights\model.pt")

        coordinator._runner = failed_runner
        failed_create = self.client.post("/api/v1/detector-probes", json=self._request())
        self.assertEqual(202, failed_create.status_code, failed_create.text)
        failed_job = failed_create.json()
        self.development.execute_probe(failed_job["job_id"])
        failed_response = self.client.get(failed_job["status_url"])

        self.assertEqual(200, failed_response.status_code, failed_response.text)
        self.assertEqual("failed", failed_response.json()["status"])
        self.assertEqual("probe_failed", failed_response.json()["error_code"])
        self.assertNotIn("error_message", failed_response.json())
        self.assertNotIn("private", failed_response.text)

        def blocked_runner(*_args, **_kwargs):
            raise DetectorDevelopmentError("source_changed", r"source changed at C:\private\source.mp4")

        coordinator._runner = blocked_runner
        retry_request = self._request(retry_from_job_id=failed_job["job_id"])
        blocked_create = self.client.post("/api/v1/detector-probes", json=retry_request)
        self.assertEqual(202, blocked_create.status_code, blocked_create.text)
        blocked_job = blocked_create.json()
        self.development.execute_probe(blocked_job["job_id"])
        blocked_response = self.client.get(blocked_job["status_url"])

        self.assertEqual(200, blocked_response.status_code, blocked_response.text)
        self.assertEqual("blocked", blocked_response.json()["status"])
        self.assertEqual("source_changed", blocked_response.json()["blocker_code"])
        self.assertNotIn("error_message", blocked_response.json())
        self.assertNotIn("private", blocked_response.text)

    def test_default_frames_are_bounded_and_explicit_frames_must_belong_to_parent(self) -> None:
        defaulted = self.client.post(
            "/api/v1/detector-probes",
            json={key: value for key, value in self._request().items() if key != "frame_indices"},
        )
        self.assertEqual(202, defaulted.status_code, defaulted.text)
        frozen = self.client.get(defaulted.json()["status_url"]).json()["frozen_request"]
        self.assertEqual([100, 102, 104, 105, 107, 109], frozen["frame_indices"])

        outside = self.client.post("/api/v1/detector-probes", json=self._request(frame_indices=[99, 100]))
        self.assertEqual(400, outside.status_code)
        self.assertEqual("probe_frames_outside_parent_trial", outside.json()["detail"]["code"])


if __name__ == "__main__":
    unittest.main()
