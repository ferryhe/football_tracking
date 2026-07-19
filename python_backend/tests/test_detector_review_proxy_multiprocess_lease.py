from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import football_tracking.detector_review_proxy as review_proxy_module
from football_tracking.detector_development_common import DetectorDevelopmentError
from football_tracking.detector_review_proxy import DetectorReviewProxyCoordinator


def _hold_claimed_job(
    repo_root: str,
    job_id: str,
    reached_stage,
    stage: str,
) -> None:
    coordinator = DetectorReviewProxyCoordinator(
        Path(repo_root),
        runner=lambda *_args: {},
        verifier=lambda *_args: None,
        auto_start_workers=False,
        output_hard_limit_bytes=1,
        disk_reserve_bytes=0,
    )

    def hold_running(_request, staging, should_cancel, _progress):
        (staging / "live-owner.marker").write_bytes(b"live")
        reached_stage.set()
        while True:
            should_cancel()
            time.sleep(0.01)

    if stage == "running":
        coordinator._runner = hold_running
    else:
        staged: dict[str, Path] = {}

        def finish_runner(_request, staging, _should_cancel, _progress):
            staged["path"] = staging
            return {"staging": str(staging)}

        coordinator._runner = finish_runner
        coordinator._seal_result = lambda *_args: ({}, {})

        def validate(root, _record):
            manifest = root / "detector_review_proxy_manifest.v1.json"
            digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            return {}, digest

        coordinator._validate_result_tree = validate
        real_execution_request = coordinator._execution_request
        request_count = 0

        def hold_before_publish(record, should_cancel):
            nonlocal request_count
            request_count += 1
            request = real_execution_request(record, should_cancel)
            if request_count == 3:
                (staged["path"] / "live-owner.marker").write_bytes(b"live")
                reached_stage.set()
                while True:
                    should_cancel()
                    time.sleep(0.01)
            return request

        coordinator._execution_request = hold_before_publish

    coordinator.execute_proxy(job_id)


def _attempt_to_steal(repo_root: str, job_id: str, contender_ready, runner_started) -> None:
    def runner(*_args):
        runner_started.set()
        return {}

    coordinator = DetectorReviewProxyCoordinator(
        Path(repo_root),
        runner=runner,
        verifier=lambda *_args: None,
        auto_start_workers=False,
        output_hard_limit_bytes=1,
        disk_reserve_bytes=0,
    )
    coordinator._seal_result = lambda *_args: ({}, {})

    def validate(root, _record):
        manifest = root / "detector_review_proxy_manifest.v1.json"
        return {}, hashlib.sha256(manifest.read_bytes()).hexdigest()

    coordinator._validate_result_tree = validate
    try:
        contender_ready.set()
        coordinator.execute_proxy(job_id)
    finally:
        coordinator.close()


def _recover_once(repo_root: str, job_id: str, start, outcomes) -> None:
    start.wait(15)
    coordinator = DetectorReviewProxyCoordinator(
        Path(repo_root),
        auto_start_workers=False,
        output_hard_limit_bytes=1,
        disk_reserve_bytes=0,
    )
    try:
        record = coordinator.get_proxy(job_id)
        raw = json.loads((coordinator._jobs_root / f"{job_id}.json").read_text(encoding="utf-8"))
        outcomes.put((record["status"], raw["record_generation"]))
    finally:
        coordinator.close()


def _observe_status(repo_root: str, job_id: str, outcome) -> None:
    coordinator = DetectorReviewProxyCoordinator(
        Path(repo_root),
        auto_start_workers=False,
        output_hard_limit_bytes=1,
        disk_reserve_bytes=0,
    )
    try:
        outcome.send(coordinator.get_proxy(job_id)["status"])
    finally:
        outcome.close()
        coordinator.close()


def _create_owner_then_die(repo_root: str, owner_path_connection) -> None:
    coordinator = DetectorReviewProxyCoordinator(
        Path(repo_root),
        auto_start_workers=False,
        output_hard_limit_bytes=1,
        disk_reserve_bytes=0,
    )
    owner_path_connection.send(str(coordinator._owner_lease_dir))
    owner_path_connection.close()
    os._exit(73)


def _configure_fake_success(coordinator: DetectorReviewProxyCoordinator) -> None:
    coordinator._execution_request = lambda record, _should_cancel: dict(record["frozen_request"])
    coordinator._runner = lambda *_args: {}
    coordinator._seal_result = lambda *_args: ({"synthetic": True}, {})

    def validate(root: Path, _record):
        manifest = root / "detector_review_proxy_manifest.v1.json"
        return {"synthetic": True}, hashlib.sha256(manifest.read_bytes()).hexdigest()

    coordinator._validate_result_tree = validate


def _crash_during_fake_success(
    repo_root: str,
    job_id: str,
    crash_stage: str,
    reached,
    owner_path_connection,
) -> None:
    coordinator = DetectorReviewProxyCoordinator(
        Path(repo_root),
        auto_start_workers=False,
        output_hard_limit_bytes=1,
        disk_reserve_bytes=0,
    )
    _configure_fake_success(coordinator)
    owner_path_connection.send(str(coordinator._owner_lease_dir))
    owner_path_connection.close()
    if crash_stage == "after_publish":
        real_publish = review_proxy_module._publish_staging_directory

        def crash_after_publish(staging: Path, destination: Path) -> None:
            real_publish(staging, destination)
            reached.set()
            os._exit(74)

        review_proxy_module._publish_staging_directory = crash_after_publish
    else:
        real_persist = coordinator._persist_record

        def crash_after_ready_persist(record: dict) -> None:
            real_persist(record)
            if record.get("status") == "ready":
                reached.set()
                os._exit(75)

        coordinator._persist_record = crash_after_ready_persist
    coordinator.execute_proxy(job_id)


def _hold_native_lease(lease_dir: str, trusted_root: str, ready, release) -> None:
    lease = review_proxy_module._HardenedLease(
        Path(lease_dir),
        trusted_root=Path(trusted_root),
        label="execution",
    )
    held = lease.acquire(blocking=False)
    if held is None:
        return
    ready.set()
    release.wait(15)
    held.release()


class DetectorReviewProxyMultiprocessLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo_root = Path(self.temporary.name).resolve()
        (self.repo_root / "data").mkdir()
        self.source = self.repo_root / "data" / "source.mp4"
        self.source.write_bytes(b"source-bound-for-lease-test")
        self.request = {
            "source_id": "source-lease-test",
            "source_relative_path": "data/source.mp4",
            "source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            "source_size_bytes": self.source.stat().st_size,
            "source_width": 512,
            "source_height": 144,
            "source_frame_count": 4,
            "source_fps": 5.0,
            "sampled_frame_indices": [0],
        }
        creator = DetectorReviewProxyCoordinator(
            self.repo_root,
            auto_start_workers=False,
            output_hard_limit_bytes=1,
            disk_reserve_bytes=0,
        )
        try:
            self.job_id = creator.create_proxy(self.request)["repair_id"]
        finally:
            creator.close()

    def test_live_foreign_process_cannot_be_recovered_or_publish_concurrently(self) -> None:
        for stage in ("running", "committing"):
            with self.subTest(stage=stage):
                self._exercise_live_owner(stage)

    def test_two_simultaneous_recoverers_advance_generation_exactly_once(self) -> None:
        context = multiprocessing.get_context("spawn")
        reached = context.Event()
        owner = context.Process(
            target=_hold_claimed_job,
            args=(str(self.repo_root), self.job_id, reached, "running"),
        )
        owner.start()
        self.assertTrue(reached.wait(15), "owner did not reach running")
        before = json.loads((self._jobs_root() / f"{self.job_id}.json").read_text(encoding="utf-8"))
        owner.terminate()
        owner.join(10)
        self.assertFalse(owner.is_alive())

        start = context.Event()
        outcomes = context.Queue()
        recoverers = [
            context.Process(target=_recover_once, args=(str(self.repo_root), self.job_id, start, outcomes))
            for _ in range(2)
        ]
        for process in recoverers:
            process.start()
        start.set()
        for process in recoverers:
            process.join(20)
            self.assertEqual(0, process.exitcode)
        results = [outcomes.get(timeout=5) for _ in recoverers]
        observed_statuses = {status for status, _generation in results}
        self.assertIn("queued", observed_statuses)
        self.assertLessEqual(observed_statuses, {"running", "queued"})
        final = json.loads((self._jobs_root() / f"{self.job_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(before["record_generation"] + 1, final["record_generation"])
        self.assertEqual("recovered_after_restart", final["stage"])
        self.assertFalse(any(self._results_root().glob(f".{self.job_id}.staging-*")))

    def test_close_retains_owner_lease_until_manual_execution_drains(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        coordinator = DetectorReviewProxyCoordinator(
            self.repo_root,
            auto_start_workers=False,
            output_hard_limit_bytes=1,
            disk_reserve_bytes=0,
        )

        def runner(_request, _staging, _should_cancel, _progress):
            entered.set()
            self.assertTrue(release.wait(15), "test did not release the runner")
            return {}

        coordinator._runner = runner
        owner_dir = coordinator._owner_lease_dir
        worker = threading.Thread(target=coordinator.execute_proxy, args=(self.job_id,), daemon=True)
        worker.start()
        self.assertTrue(entered.wait(15), "manual execution did not start")
        coordinator.close()
        self.assertIsNotNone(coordinator._owner_lease)
        self.assertTrue(owner_dir.is_dir())

        context = multiprocessing.get_context("spawn")
        status_reader, status_writer = context.Pipe(duplex=False)
        observer = context.Process(
            target=_observe_status,
            args=(str(self.repo_root), self.job_id, status_writer),
        )
        observer.start()
        self.assertTrue(status_reader.poll(15), "foreign observer did not report status")
        self.assertEqual("running", status_reader.recv())
        observer.join(15)
        self.assertEqual(0, observer.exitcode)
        release.set()
        worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(coordinator._owner_lease)
        self.assertFalse(owner_dir.exists())

    def test_startup_sweeps_dead_unreferenced_owner_generation(self) -> None:
        context = multiprocessing.get_context("spawn")
        parent_connection, child_connection = context.Pipe(duplex=False)
        owner = context.Process(
            target=_create_owner_then_die,
            args=(str(self.repo_root), child_connection),
        )
        owner.start()
        self.assertTrue(parent_connection.poll(15), "dead owner did not report its lease path")
        owner_dir = Path(parent_connection.recv())
        owner.join(15)
        self.assertEqual(73, owner.exitcode)
        self.assertTrue(owner_dir.is_dir())

        sweeper = DetectorReviewProxyCoordinator(
            self.repo_root,
            auto_start_workers=False,
            output_hard_limit_bytes=1,
            disk_reserve_bytes=0,
        )
        try:
            self.assertFalse(owner_dir.exists())
        finally:
            sweeper.close()

    def test_real_process_death_after_publish_recovers_ready_once(self) -> None:
        self._exercise_real_crash_recovery("after_publish", 74)

    def test_real_process_death_after_ready_persist_keeps_ready_and_sweeps_owner(self) -> None:
        self._exercise_real_crash_recovery("after_ready", 75)

    def test_generation_change_before_failure_preserves_foreign_state_and_staging(self) -> None:
        self._exercise_generation_fence("before_failure_cleanup", destination_expected=False)

    def test_generation_change_before_publish_prevents_rename_and_cleanup(self) -> None:
        self._exercise_generation_fence("before_publish", destination_expected=False)

    def test_generation_change_after_publish_prevents_stale_cleanup_or_finalize(self) -> None:
        self._exercise_generation_fence("after_publish", destination_expected=True)

    def test_generation_change_before_finalize_preserves_published_result(self) -> None:
        self._exercise_generation_fence("before_finalize", destination_expected=True)

    def test_generation_change_before_recovery_cleanup_preserves_orphan_artifacts(self) -> None:
        context = multiprocessing.get_context("spawn")
        reached = context.Event()
        owner = context.Process(
            target=_hold_claimed_job,
            args=(str(self.repo_root), self.job_id, reached, "running"),
        )
        owner.start()
        self.assertTrue(reached.wait(15), "owner did not reach running")
        staging = self._single_staging()
        owner.terminate()
        owner.join(15)
        self.assertFalse(owner.is_alive())

        execution = review_proxy_module._HardenedLease(
            self._root() / "leases" / "execution",
            trusted_root=self._root() / "leases",
            label="execution",
        )
        held = execution.acquire(blocking=False)
        self.assertIsNotNone(held)
        recovery = None
        try:
            recovery = DetectorReviewProxyCoordinator(
                self.repo_root,
                auto_start_workers=False,
                output_hard_limit_bytes=1,
                disk_reserve_bytes=0,
            )
        finally:
            held.release()
        injected = False

        def mutate_generation(stage: str, job_id: str) -> None:
            nonlocal injected
            if injected or stage != "before_recovery_cleanup":
                return
            injected = True
            path = self._jobs_root() / f"{job_id}.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            record["record_generation"] += 1
            record["foreign_generation_marker"] = stage
            review_proxy_module.atomic_write_json(path, record, trusted_root=self._jobs_root())

        recovery._coordination_failpoint = mutate_generation
        try:
            recovery._load_and_recover_jobs()
            self.assertTrue(injected)
            record = json.loads((self._jobs_root() / f"{self.job_id}.json").read_text(encoding="utf-8"))
            self.assertEqual("running", record["status"])
            self.assertEqual("before_recovery_cleanup", record["foreign_generation_marker"])
            self.assertTrue(staging.is_dir())
            self.assertTrue((staging / "live-owner.marker").is_file())
        finally:
            recovery.close()

    def _exercise_real_crash_recovery(self, crash_stage: str, expected_exitcode: int) -> None:
        context = multiprocessing.get_context("spawn")
        reached = context.Event()
        parent_connection, child_connection = context.Pipe(duplex=False)
        worker = context.Process(
            target=_crash_during_fake_success,
            args=(str(self.repo_root), self.job_id, crash_stage, reached, child_connection),
        )
        worker.start()
        self.assertTrue(parent_connection.poll(15), "crashing owner did not report its lease path")
        owner_dir = Path(parent_connection.recv())
        self.assertTrue(reached.wait(15), f"owner did not reach {crash_stage}")
        worker.join(15)
        self.assertEqual(expected_exitcode, worker.exitcode)
        before = json.loads((self._jobs_root() / f"{self.job_id}.json").read_text(encoding="utf-8"))
        expected_before_status = "committing" if crash_stage == "after_publish" else "ready"
        self.assertEqual(expected_before_status, before["status"])
        self.assertTrue(self._destination().is_dir())

        execution = review_proxy_module._HardenedLease(
            self._root() / "leases" / "execution",
            trusted_root=self._root() / "leases",
            label="execution",
        )
        held = execution.acquire(blocking=False)
        self.assertIsNotNone(held)
        recovered = None
        try:
            recovered = DetectorReviewProxyCoordinator(
                self.repo_root,
                auto_start_workers=False,
                output_hard_limit_bytes=1,
                disk_reserve_bytes=0,
            )
            _configure_fake_success(recovered)
        finally:
            held.release()
        try:
            recovered._load_and_recover_jobs()
            ready = recovered.get_proxy(self.job_id)
            self.assertEqual("ready", ready["status"])
            after = json.loads((self._jobs_root() / f"{self.job_id}.json").read_text(encoding="utf-8"))
            expected_increment = 1 if crash_stage == "after_publish" else 0
            self.assertEqual(before["record_generation"] + expected_increment, after["record_generation"])
            self.assertFalse(owner_dir.exists())
        finally:
            recovered.close()

    def _exercise_generation_fence(self, target_stage: str, *, destination_expected: bool) -> None:
        coordinator = DetectorReviewProxyCoordinator(
            self.repo_root,
            auto_start_workers=False,
            output_hard_limit_bytes=1,
            disk_reserve_bytes=0,
        )
        _configure_fake_success(coordinator)
        if target_stage == "before_failure_cleanup":

            def fail_runner(_request, staging, _should_cancel, _progress):
                (staging / "preserve.marker").write_bytes(b"preserve")
                raise RuntimeError("synthetic runner failure")

            coordinator._runner = fail_runner
        injected = False

        def mutate_generation(stage: str, job_id: str) -> None:
            nonlocal injected
            if injected or stage != target_stage:
                return
            injected = True
            path = self._jobs_root() / f"{job_id}.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            record["record_generation"] += 1
            record["foreign_generation_marker"] = target_stage
            review_proxy_module.atomic_write_json(path, record, trusted_root=self._jobs_root())

        coordinator._coordination_failpoint = mutate_generation
        try:
            coordinator.execute_proxy(self.job_id)
            self.assertTrue(injected)
            record = json.loads((self._jobs_root() / f"{self.job_id}.json").read_text(encoding="utf-8"))
            self.assertEqual(target_stage, record["foreign_generation_marker"])
            self.assertIn(record["status"], {"running", "committing"})
            self.assertEqual(destination_expected, self._destination().is_dir())
            staging = list(self._results_root().glob(f".{self.job_id}.staging-*"))
            self.assertEqual(not destination_expected, bool(staging))
        finally:
            coordinator.close()

    def _exercise_live_owner(self, stage: str) -> None:
        context = multiprocessing.get_context("spawn")
        reached_stage = context.Event()
        owner = context.Process(
            target=_hold_claimed_job,
            args=(str(self.repo_root), self.job_id, reached_stage, stage),
        )
        owner.start()
        observer = None
        contender = None
        try:
            self.assertTrue(reached_stage.wait(15), f"owner did not reach {stage}")
            staging = self._single_staging()
            marker = staging / "live-owner.marker"
            self.assertTrue(marker.is_file())
            owned_record = json.loads((self._jobs_root() / f"{self.job_id}.json").read_text(encoding="utf-8"))
            self.assertRegex(owned_record["owner_id"], r"^proxy-owner-[0-9a-f]{32}$")
            self.assertRegex(owned_record["owner_generation"], r"^proxy-generation-[0-9a-f]{32}$")
            self.assertIsInstance(owned_record["owner_heartbeat_at"], str)
            owned_record["owner_heartbeat_at"] = "2000-01-01T00:00:00Z"
            owned_record["record_generation"] += 1
            review_proxy_module.atomic_write_json(
                self._jobs_root() / f"{self.job_id}.json",
                owned_record,
                trusted_root=self._jobs_root(),
            )

            observer = DetectorReviewProxyCoordinator(
                self.repo_root,
                auto_start_workers=False,
                output_hard_limit_bytes=1,
                disk_reserve_bytes=0,
            )
            observed = observer.get_proxy(self.job_id)
            self.assertEqual(stage, observed["status"])
            still_owned = json.loads((self._jobs_root() / f"{self.job_id}.json").read_text(encoding="utf-8"))
            self.assertEqual("2000-01-01T00:00:00Z", still_owned["owner_heartbeat_at"])
            self.assertTrue(marker.is_file(), "startup deleted a live owner's staging tree")

            runner_started = context.Event()
            contender_ready = context.Event()
            contender = context.Process(
                target=_attempt_to_steal,
                args=(str(self.repo_root), self.job_id, contender_ready, runner_started),
            )
            contender.start()
            self.assertTrue(contender_ready.wait(15), "foreign process did not reach execution attempt")
            self.assertFalse(runner_started.wait(0.75), "foreign process executed the live job")
            self.assertTrue(contender.is_alive(), "foreign execution attempt exited instead of waiting for the lease")
            self.assertFalse(self._destination().exists(), "foreign process published a concurrent result")
            self.assertTrue(marker.is_file())
        finally:
            if contender is not None:
                contender.terminate()
                contender.join(10)
            if observer is not None:
                observer.close()
            owner.terminate()
            owner.join(10)

        self.assertFalse(owner.is_alive())
        recovered = DetectorReviewProxyCoordinator(
            self.repo_root,
            auto_start_workers=False,
            output_hard_limit_bytes=1,
            disk_reserve_bytes=0,
        )
        try:
            record = recovered.get_proxy(self.job_id)
            self.assertEqual("queued", record["status"])
            self.assertEqual("recovered_after_restart", record["stage"])
            self.assertFalse(any(self._results_root().glob(f".{self.job_id}.staging-*")))
            recovered_record = json.loads((self._jobs_root() / f"{self.job_id}.json").read_text(encoding="utf-8"))
            self.assertIsNone(recovered_record["owner_id"])
            self.assertIsNone(recovered_record["owner_generation"])
            self.assertIsNone(recovered_record["owner_heartbeat_at"])
        finally:
            recovered.close()

    def _single_staging(self) -> Path:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            candidates = list(self._results_root().glob(f".{self.job_id}.staging-*"))
            if len(candidates) == 1:
                return candidates[0]
            time.sleep(0.01)
        raise AssertionError("expected exactly one live staging tree")

    def test_dispatcher_refresh_lifetime_keeps_owner_lease_until_iteration_drains(self) -> None:
        coordinator = DetectorReviewProxyCoordinator(
            self.repo_root,
            auto_start_workers=False,
            output_hard_limit_bytes=1,
            disk_reserve_bytes=0,
        )
        entered_refresh = threading.Event()
        release_refresh = threading.Event()
        coordinator._execute_in_global_slot = lambda _job_id: False

        def blocked_refresh() -> None:
            entered_refresh.set()
            self.assertTrue(release_refresh.wait(5), "dispatcher refresh was not released")

        coordinator._refresh_jobs_from_disk = blocked_refresh
        coordinator._auto_start_workers = True
        coordinator._start_dispatcher()
        owner_lease = coordinator._owner_lease
        self.assertIsNotNone(owner_lease)
        try:
            self.assertTrue(entered_refresh.wait(5), "dispatcher did not enter trailing refresh")
            with mock.patch.object(coordinator._dispatcher, "join", return_value=None):
                coordinator.close()
            self.assertIsNotNone(coordinator._owner_lease)
            self.assertFalse(owner_lease._released)
            contender = coordinator._owner_lease_object.acquire(blocking=False)
            self.assertIsNone(contender, "close released the owner while dispatcher refresh was active")

            release_refresh.set()
            coordinator._dispatcher.join(5)
            self.assertFalse(coordinator._dispatcher.is_alive())
            self.assertIsNone(coordinator._owner_lease)
            self.assertTrue(owner_lease._released)
        finally:
            release_refresh.set()
            if coordinator._dispatcher is not None:
                coordinator._dispatcher.join(5)
            coordinator.close()

    def test_dispatcher_retries_transient_refresh_error_on_same_thread(self) -> None:
        coordinator = DetectorReviewProxyCoordinator(
            self.repo_root,
            auto_start_workers=False,
            output_hard_limit_bytes=1,
            disk_reserve_bytes=0,
        )
        _configure_fake_success(coordinator)
        refresh_thread: list[int] = []
        runner_thread: list[int] = []
        original_refresh = coordinator._refresh_jobs_from_disk
        injected = False

        def flaky_refresh() -> None:
            nonlocal injected
            if threading.current_thread().name == "detector-review-proxy-dispatcher" and not injected:
                injected = True
                refresh_thread.append(threading.get_ident())
                raise DetectorDevelopmentError("path_unavailable", "synthetic transient refresh failure")
            original_refresh()

        def runner(*_args):
            runner_thread.append(threading.get_ident())
            return {}

        coordinator._refresh_jobs_from_disk = flaky_refresh
        coordinator._runner = runner
        coordinator._auto_start_workers = True
        coordinator._start_dispatcher()
        coordinator._dispatch_event.set()
        try:
            deadline = time.monotonic() + 10
            while coordinator.get_proxy(self.job_id)["status"] != "ready":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            self.assertTrue(injected)
            self.assertEqual(1, len(refresh_thread))
            self.assertEqual(refresh_thread, runner_thread)
            self.assertTrue(coordinator._dispatcher.is_alive())
        finally:
            coordinator.close()
        self.assertFalse(coordinator._dispatcher.is_alive())

    def test_orphan_probe_lease_is_released_when_job_lease_construction_fails(self) -> None:
        coordinator = DetectorReviewProxyCoordinator(
            self.repo_root,
            auto_start_workers=False,
            output_hard_limit_bytes=1,
            disk_reserve_bytes=0,
        )
        dead_owner_dir = coordinator._owner_leases_root / "owner-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        dead_owner_dir.mkdir()
        dead_owner_lease = review_proxy_module._HardenedLease(
            dead_owner_dir,
            trusted_root=coordinator._leases_root,
            label="owner",
        )
        dead_owner_held = dead_owner_lease.acquire(blocking=False)
        self.assertIsNotNone(dead_owner_held)
        execution_held = coordinator._try_acquire_execution_lease()
        self.assertIsNotNone(execution_held)
        snapshot = {
            "job_id": self.job_id,
            "coordination_bindings": coordinator._coordination_bindings(),
        }
        coordinator._probe_owner_lease = lambda _record: (False, dead_owner_held, dead_owner_lease)
        coordinator._job_lease = mock.Mock(
            side_effect=DetectorDevelopmentError("unsafe_result_tree", "synthetic job lease failure")
        )
        try:
            with self.assertRaisesRegex(DetectorDevelopmentError, "synthetic job lease failure"):
                coordinator._recover_orphaned_job(snapshot, execution_held)
            self.assertTrue(dead_owner_held._released)
            reacquired = dead_owner_lease.acquire(blocking=False)
            self.assertIsNotNone(reacquired)
            reacquired.release()
        finally:
            if not dead_owner_held._released:
                dead_owner_held.release()
            execution_held.release()
            coordinator.close()

    def _root(self) -> Path:
        return self.repo_root / "data" / "ball_detector_development_v1" / "review_proxies"

    def _results_root(self) -> Path:
        return self._root() / "results"

    def _jobs_root(self) -> Path:
        return self._root() / "jobs"

    def _destination(self) -> Path:
        return self._results_root() / self.job_id


class HardenedLeaseSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.leases = self.root / "leases"
        self.leases.mkdir()

    def test_each_coordination_object_is_exclusive_and_released(self) -> None:
        for label in ("registry", "execution", "job", "owner"):
            with self.subTest(label=label):
                lease_dir = self.leases / label
                lease_dir.mkdir()
                lease = review_proxy_module._HardenedLease(
                    lease_dir,
                    trusted_root=self.leases,
                    label=label,
                )
                held = lease.acquire(blocking=False)
                self.assertIsNotNone(held)
                contender = review_proxy_module._HardenedLease(
                    lease_dir,
                    trusted_root=self.leases,
                    label=label,
                )
                self.assertIsNone(contender.acquire(blocking=False))
                held.release()
                reacquired = contender.acquire(blocking=False)
                self.assertIsNotNone(reacquired)
                reacquired.release()

    def test_death_releases_native_lease(self) -> None:
        context = multiprocessing.get_context("spawn")
        lease_dir = self.leases / "execution"
        lease_dir.mkdir()
        ready = context.Event()
        release = context.Event()
        owner = context.Process(
            target=_hold_native_lease,
            args=(str(lease_dir), str(self.leases), ready, release),
        )
        owner.start()
        self.assertTrue(ready.wait(15), "native lease owner did not start")
        owner.terminate()
        owner.join(15)
        contender = review_proxy_module._HardenedLease(
            lease_dir,
            trusted_root=self.leases,
            label="execution",
        )
        held = contender.acquire(blocking=False)
        self.assertIsNotNone(held)
        held.release()

    def test_parent_swap_during_open_is_rejected(self) -> None:
        for label in ("registry", "execution", "job", "owner"):
            with self.subTest(label=label):
                parent = self.leases / f"parents-{label}"
                parent.mkdir()
                lease_dir = parent / label
                lease_dir.mkdir()
                moved = self.leases / f"parents-{label}-original"

                def swap_parent(_target: Path) -> None:
                    parent.rename(moved)
                    parent.mkdir()
                    (parent / label).mkdir()

                lease = review_proxy_module._HardenedLease(
                    lease_dir,
                    trusted_root=self.leases,
                    label=label,
                    after_open_hook=swap_parent,
                )
                with self.assertRaises(DetectorDevelopmentError):
                    lease.acquire(blocking=False)

    def test_replaceable_child_never_splits_native_lease(self) -> None:
        for label in ("registry", "execution", "job", "owner"):
            with self.subTest(label=label):
                lease_dir = self.leases / f"child-{label}"
                lease_dir.mkdir()
                lease = review_proxy_module._HardenedLease(
                    lease_dir,
                    trusted_root=self.leases,
                    label=label,
                )
                held = lease.acquire(blocking=False)
                self.assertIsNotNone(held)
                if os.name == "nt":
                    with self.assertRaises(OSError):
                        os.replace(lease.lock_path, self.root / f"replaced-{label}.lock")
                else:
                    lease.lock_path.write_bytes(b"replaceable-child")
                    lease.lock_path.unlink()
                    lease.lock_path.write_bytes(b"replacement-child")
                contender = review_proxy_module._HardenedLease(
                    lease_dir,
                    trusted_root=self.leases,
                    label=label,
                )
                self.assertIsNone(contender.acquire(blocking=False))
                held.release()

    def test_target_directory_replacement_is_detected_or_denied(self) -> None:
        for label in ("registry", "execution", "job", "owner"):
            with self.subTest(label=label):
                lease_dir = self.leases / f"target-{label}"
                lease_dir.mkdir()
                moved = self.leases / f"target-{label}-original"
                lease = review_proxy_module._HardenedLease(
                    lease_dir,
                    trusted_root=self.leases,
                    label=label,
                )
                held = lease.acquire(blocking=False)
                self.assertIsNotNone(held)
                if os.name == "nt":
                    with self.assertRaises(OSError):
                        lease_dir.rename(moved)
                    held.validate()
                else:
                    lease_dir.rename(moved)
                    lease_dir.mkdir()
                    with self.assertRaises(DetectorDevelopmentError):
                        held.validate()
                    with self.assertRaises(DetectorDevelopmentError):
                        lease.acquire(blocking=False)
                held.release()

    def test_after_open_exception_releases_every_handle(self) -> None:
        lease_dir = self.leases / "exception"
        lease_dir.mkdir()

        def fail_after_open(_target: Path) -> None:
            raise RuntimeError("synthetic after-open failure")

        lease = review_proxy_module._HardenedLease(
            lease_dir,
            trusted_root=self.leases,
            label="execution",
            after_open_hook=fail_after_open,
        )
        with self.assertRaisesRegex(RuntimeError, "after-open"):
            lease.acquire(blocking=False)
        contender = review_proxy_module._HardenedLease(
            lease_dir,
            trusted_root=self.leases,
            label="execution",
        )
        held = contender.acquire(blocking=False)
        self.assertIsNotNone(held)
        held.release()

    def test_symlink_lease_is_rejected_without_touching_target(self) -> None:
        external = self.root / "external"
        external.mkdir()
        marker = external / "marker"
        marker.write_bytes(b"external")
        lease_dir = self.leases / "owner"
        try:
            os.symlink(external, lease_dir, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink creation is unavailable: {exc}")
        with self.assertRaises(DetectorDevelopmentError):
            lease = review_proxy_module._HardenedLease(
                lease_dir,
                trusted_root=self.leases,
                label="owner",
            )
            lease.acquire(blocking=False)
        self.assertEqual(b"external", marker.read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows lock files are the native lease target")
    def test_hardlinked_lock_file_is_rejected_without_mutating_external_file(self) -> None:
        for label in ("registry", "execution", "job", "owner"):
            with self.subTest(label=label):
                external = self.root / f"external-{label}.lock"
                external.write_bytes(b"do-not-touch")
                lease_dir = self.leases / f"hardlink-{label}"
                lease_dir.mkdir()
                os.link(external, lease_dir / "coordination.lock")
                lease = review_proxy_module._HardenedLease(
                    lease_dir,
                    trusted_root=self.leases,
                    label=label,
                )
                with self.assertRaises(DetectorDevelopmentError):
                    lease.acquire(blocking=False)
                self.assertEqual(b"do-not-touch", external.read_bytes())

    def test_guarded_publish_rejects_staging_swap_without_mutating_replacement(self) -> None:
        parent = self.root / "results"
        parent.mkdir()
        staging = parent / ".job.staging"
        staging.mkdir()
        (staging / "owned.marker").write_bytes(b"owned")
        replacement = parent / "replacement"
        replacement.mkdir()
        (replacement / "external.marker").write_bytes(b"external")
        original = parent / "original-staging"
        destination = parent / "job"

        def swap(_source: Path) -> None:
            staging.rename(original)
            replacement.rename(staging)

        with self.assertRaises(DetectorDevelopmentError):
            review_proxy_module._publish_staging_directory(staging, destination, attack_hook=swap)
        self.assertFalse(destination.exists())
        self.assertEqual(b"external", (staging / "external.marker").read_bytes())
        self.assertEqual(b"owned", (original / "owned.marker").read_bytes())

    def test_guarded_delete_rejects_target_swap_without_mutating_replacement(self) -> None:
        parent = self.root / "results"
        parent.mkdir()
        candidate = parent / "job"
        candidate.mkdir()
        (candidate / "owned.marker").write_bytes(b"owned")
        replacement = parent / "replacement"
        replacement.mkdir()
        (replacement / "external.marker").write_bytes(b"external")
        original = parent / "original-job"

        def swap(_source: Path) -> None:
            candidate.rename(original)
            replacement.rename(candidate)

        removed = review_proxy_module._safe_remove_tree(candidate, parent, attack_hook=swap)
        self.assertFalse(removed)
        self.assertEqual(b"external", (candidate / "external.marker").read_bytes())
        self.assertEqual(b"owned", (original / "owned.marker").read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows delete-sharing semantics")
    def test_guarded_delete_rejects_post_quarantine_swap_before_any_mutation(self) -> None:
        parent = self.root / "results"
        parent.mkdir()
        candidate = parent / "job"
        candidate.mkdir()
        (candidate / "owned.marker").write_bytes(b"owned")
        external = parent / "external"
        external.mkdir()
        external_marker = external / "external.marker"
        external_marker.write_bytes(b"external")
        external_identity = (external.stat().st_dev, external.stat().st_ino)
        parked_owned = parent / "parked-owned"

        def swap(quarantine: Path) -> None:
            quarantine.rename(parked_owned)
            external.rename(quarantine)

        removed = review_proxy_module._safe_remove_tree(
            candidate,
            parent,
            post_quarantine_hook=swap,
        )
        quarantines = list(parent.glob(".delete-job-*"))
        self.assertFalse(removed)
        self.assertEqual(1, len(quarantines))
        swapped_external = quarantines[0]
        self.assertEqual(external_identity, (swapped_external.stat().st_dev, swapped_external.stat().st_ino))
        self.assertEqual(b"external", (swapped_external / "external.marker").read_bytes())
        self.assertEqual(b"owned", (parked_owned / "owned.marker").read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows delete-sharing semantics")
    def test_guarded_delete_pins_quarantine_before_post_pin_attack(self) -> None:
        parent = self.root / "results"
        parent.mkdir()
        candidate = parent / "job"
        candidate.mkdir()
        (candidate / "owned.marker").write_bytes(b"owned")
        external = parent / "external"
        external.mkdir()
        external_marker = external / "external.marker"
        external_marker.write_bytes(b"external")
        moved_quarantine = parent / "moved-quarantine"
        attack_attempted = False

        def swap(quarantine: Path) -> None:
            nonlocal attack_attempted
            attack_attempted = True
            with self.assertRaises(OSError):
                quarantine.rename(moved_quarantine)
            self.assertTrue(quarantine.is_dir())
            self.assertTrue(external.is_dir())

        removed = review_proxy_module._safe_remove_tree(
            candidate,
            parent,
            after_quarantine_pin_hook=swap,
        )
        self.assertTrue(attack_attempted)
        self.assertTrue(removed)
        self.assertFalse(candidate.exists())
        self.assertFalse(moved_quarantine.exists())
        self.assertEqual(b"external", external_marker.read_bytes())

    def test_guarded_delete_rejects_hardlink_before_any_mutation(self) -> None:
        parent = self.root / "results"
        parent.mkdir()
        candidate = parent / "job"
        candidate.mkdir()
        external = self.root / "external.bin"
        external.write_bytes(b"external")
        os.link(external, candidate / "alias.bin")
        self.assertFalse(review_proxy_module._safe_remove_tree(candidate, parent))
        self.assertTrue(candidate.is_dir())
        self.assertEqual(b"external", external.read_bytes())


if __name__ == "__main__":
    unittest.main()
