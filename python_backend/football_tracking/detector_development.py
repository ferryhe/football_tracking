from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from football_tracking.detector_development_common import (
    CorruptProbeFrameError,
    DetectorDevelopmentError,
    ProbeWorkerDiedError,
)
from football_tracking.detector_model_import import (
    ensure_detector_import_roots,
    import_detector_model,
    load_imported_model_records,
)
from football_tracking.detector_model_registry import (
    build_builtin_model_catalog,
    observe_pinned_model_runtime,
)
from football_tracking.detector_probe_runner import merge_probe_candidates, normalize_probe_candidates


class DetectorDevelopmentService:
    """Thin facade joining registry/import/probe capabilities for the API service."""

    def __init__(
        self,
        repo_root: Path,
        *,
        probe_runner: Callable[..., dict[str, Any]] | None = None,
        auto_start_workers: bool = True,
        catalog_provider: Callable[[], dict[str, Any]] | None = None,
        worker_deadline_seconds: float = 20 * 60.0,
        worker_heartbeat_timeout_seconds: float = 10.0,
        worker_command_factory: Callable[[Path, Path, int], list[str]] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve(strict=True)
        ensure_detector_import_roots(self.repo_root)
        self._probe_runner = probe_runner
        self._auto_start_workers = auto_start_workers
        self._catalog_provider = catalog_provider
        self._worker_deadline_seconds = worker_deadline_seconds
        self._worker_heartbeat_timeout_seconds = worker_heartbeat_timeout_seconds
        self._worker_command_factory = worker_command_factory
        self._probe_coordinator = None
        self._lifecycle_lock = threading.RLock()
        self._catalog_import_lock = threading.RLock()
        self._closed = False

    def list_models(self) -> dict[str, Any]:
        with self._lifecycle_lock:
            self._require_open()
        with self._catalog_import_lock:
            catalog = build_builtin_model_catalog(self.repo_root)
            catalog["models"].extend(load_imported_model_records(self.repo_root))
            identities: set[tuple[str, str]] = set()
            for model in catalog["models"]:
                descriptor = model.get("descriptor") if isinstance(model, dict) else None
                if not isinstance(descriptor, dict):
                    raise DetectorDevelopmentError("invalid_registry", "Detector catalog model descriptor is invalid")
                identity = (descriptor.get("model_id"), descriptor.get("version"))
                if not all(isinstance(item, str) and item for item in identity) or identity in identities:
                    raise DetectorDevelopmentError("invalid_registry", "Detector catalog model identity is duplicated")
                identities.add(identity)
            return catalog

    def import_model(self, request: dict[str, Any]) -> dict[str, Any]:
        with self._lifecycle_lock:
            self._require_open()
        with self._catalog_import_lock:
            return import_detector_model(self.repo_root, request)

    def create_probe(
        self,
        request: dict[str, Any],
        *,
        _expected_profile_sha256s: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with self._lifecycle_lock:
            coordinator = self._probes()
        return coordinator.create_probe(
            request,
            _expected_profile_sha256s=_expected_profile_sha256s,
        )

    def get_probe(self, job_id: str) -> dict[str, Any]:
        with self._lifecycle_lock:
            coordinator = self._probes()
        return coordinator.get_probe(job_id)

    def get_verified_probe(self, job_id: str) -> dict[str, Any]:
        with self._lifecycle_lock:
            coordinator = self._probes()
        return coordinator.get_verified_probe(job_id)

    def get_verified_probe_job_record(self, job_id: str) -> dict[str, Any]:
        with self._lifecycle_lock:
            coordinator = self._probes()
        return coordinator.get_verified_probe_job_record(job_id)

    def get_review_proxy_upgrade_parent(self, job_id: str) -> dict[str, Any]:
        with self._lifecycle_lock:
            coordinator = self._probes()
        return coordinator.get_review_proxy_upgrade_parent(job_id)

    def get_review_proxy_upgrade_child(self, parent_job_id: str) -> dict[str, Any] | None:
        with self._lifecycle_lock:
            coordinator = self._probes()
        return coordinator.get_review_proxy_upgrade_child(parent_job_id)

    def create_review_proxy_upgrade_child(
        self,
        parent_job_id: str,
        *,
        repair_evidence: dict[str, Any],
        proxy_media: dict[str, Any],
        proxy_sample_bytes: dict[int, bytes],
        expected_child_plan: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lifecycle_lock:
            coordinator = self._probes()
        return coordinator.create_review_proxy_upgrade_child(
            parent_job_id,
            repair_evidence=repair_evidence,
            proxy_media=proxy_media,
            proxy_sample_bytes=proxy_sample_bytes,
            expected_child_plan=expected_child_plan,
        )

    def review_proxy_upgrade_child_plan(self, parent_job_id: str, *, repair_evidence: dict[str, Any]) -> dict[str, Any]:
        with self._lifecycle_lock:
            coordinator = self._probes()
        return coordinator.review_proxy_upgrade_child_plan(parent_job_id, repair_evidence=repair_evidence)

    def cancel_probe(self, job_id: str) -> dict[str, Any]:
        with self._lifecycle_lock:
            coordinator = self._probes()
        return coordinator.cancel_probe(job_id)

    def execute_probe(self, job_id: str) -> None:
        with self._lifecycle_lock:
            coordinator = self._probes()
        coordinator.execute_probe(job_id)

    def get_probe_artifact(self, job_id: str, artifact_id: str):
        with self._lifecycle_lock:
            coordinator = self._probes()
        return coordinator.get_probe_artifact(job_id, artifact_id)

    def read_probe_artifact(self, job_id: str, artifact_id: str) -> tuple[bytes, str, str]:
        with self._lifecycle_lock:
            coordinator = self._probes()
        return coordinator.read_probe_artifact(job_id, artifact_id)

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            coordinator = self._probe_coordinator
            self._probe_coordinator = None
        if coordinator is not None:
            coordinator.close()

    def _probes(self):
        with self._lifecycle_lock:
            self._require_open()
            if self._probe_coordinator is None:
                from football_tracking.detector_probe import DetectorProbeCoordinator

                self._probe_coordinator = DetectorProbeCoordinator(
                    self.repo_root,
                    probe_runner=self._probe_runner,
                    auto_start_workers=self._auto_start_workers,
                    catalog_provider=self._catalog_provider,
                    worker_deadline_seconds=self._worker_deadline_seconds,
                    worker_heartbeat_timeout_seconds=(self._worker_heartbeat_timeout_seconds),
                    worker_command_factory=self._worker_command_factory,
                )
            return self._probe_coordinator

    def _require_open(self) -> None:
        if self._closed:
            raise DetectorDevelopmentError("service_closed", "Detector development service is closed")


__all__ = [
    "CorruptProbeFrameError",
    "DetectorDevelopmentError",
    "DetectorDevelopmentService",
    "ProbeWorkerDiedError",
    "build_builtin_model_catalog",
    "merge_probe_candidates",
    "normalize_probe_candidates",
    "observe_pinned_model_runtime",
]
