from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from football_tracking import ball_annotation_service as service_module
from football_tracking.ball_annotation_service import (
    BallAnnotationService,
    BallAnnotationServiceError,
    _CoordinationLock,
)
from football_tracking.detector_development_common import canonical_json_bytes, canonical_sha256


def _acquire_coordination_lock_in_process(
    root: str,
    started: Any,
    acquired: Any,
) -> None:
    lock = _CoordinationLock(Path(root))
    started.set()
    with lock:
        acquired.set()


def _hold_coordination_lock_in_process(
    root: str,
    entered: Any,
    release: Any,
) -> None:
    with _CoordinationLock(Path(root)):
        entered.set()
        release.wait(30)


def _unexpected_probe(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("probe gateway must not be used during startup security tests")


def _create_directory_link(test: unittest.TestCase, link: Path, target: Path) -> None:
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            test.skipTest(f"directory junction unavailable: {completed.stderr or completed.stdout}")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        test.skipTest(f"directory symlink unavailable: {exc}")


def _remove_directory_link(link: Path) -> None:
    if not os.path.lexists(link):
        return
    if os.name == "nt":
        os.rmdir(link)
    else:
        link.unlink()


class CoordinationLockSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "trusted"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    @property
    def lock_path(self) -> Path:
        return self.root / "coordination.lock"

    def test_normal_lock_is_reentrant_and_does_not_write_sentinel(self) -> None:
        lock = _CoordinationLock(self.root)

        with lock:
            first_handle = lock._local.handle
            with lock:
                self.assertIs(first_handle, lock._local.handle)
                self.assertEqual(2, lock._local.depth)
            self.assertEqual(1, lock._local.depth)

        if os.name == "nt":
            self.assertEqual(b"", self.lock_path.read_bytes())
        else:
            self.assertFalse(self.lock_path.exists())

    def test_lock_serializes_independent_processes(self) -> None:
        context = multiprocessing.get_context("spawn")
        started = context.Event()
        acquired = context.Event()
        process = context.Process(
            target=_acquire_coordination_lock_in_process,
            args=(str(self.root), started, acquired),
        )
        lock = _CoordinationLock(self.root)
        try:
            with lock:
                process.start()
                self.assertTrue(started.wait(15), "child did not attempt lock acquisition")
                self.assertFalse(acquired.wait(0.25), "child bypassed the held coordination lock")
            self.assertTrue(acquired.wait(15), "child did not acquire the released coordination lock")
            process.join(15)
            self.assertEqual(0, process.exitcode)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(5)

    @unittest.skipIf(os.name == "nt", "POSIX locks the trusted coordination root directory")
    def test_posix_child_lock_inode_replacement_cannot_split_process_lock(self) -> None:
        context = multiprocessing.get_context("spawn")
        holder_entered = context.Event()
        release_holder = context.Event()
        contender_started = context.Event()
        contender_acquired = context.Event()
        holder = context.Process(
            target=_hold_coordination_lock_in_process,
            args=(str(self.root), holder_entered, release_holder),
        )
        contender = context.Process(
            target=_acquire_coordination_lock_in_process,
            args=(str(self.root), contender_started, contender_acquired),
        )
        self.lock_path.write_bytes(b"old-child-inode")
        try:
            holder.start()
            self.assertTrue(holder_entered.wait(15), "holder did not acquire the directory lock")
            self.lock_path.unlink()
            self.lock_path.write_bytes(b"replacement-child-inode")
            contender.start()
            self.assertTrue(contender_started.wait(15), "contender did not attempt acquisition")
            self.assertFalse(
                contender_acquired.wait(0.25),
                "replacement child inode split the coordination lock",
            )
            release_holder.set()
            self.assertTrue(contender_acquired.wait(15), "contender did not acquire released root lock")
            holder.join(15)
            contender.join(15)
            self.assertEqual(0, holder.exitcode)
            self.assertEqual(0, contender.exitcode)
        finally:
            release_holder.set()
            for process in (holder, contender):
                if process.is_alive():
                    process.terminate()
                process.join(5)

    @unittest.skipUnless(os.name == "nt", "Windows uses the child lock file handle")
    def test_preexisting_symlink_is_rejected_without_writing_target(self) -> None:
        external = Path(self.temp.name) / "external.lock"
        external.write_bytes(b"outside")
        try:
            self.lock_path.symlink_to(external)
        except OSError as exc:
            self.skipTest(f"file symlinks are unavailable: {exc}")

        with self.assertRaises(BallAnnotationServiceError) as raised:
            with _CoordinationLock(self.root):
                self.fail("unsafe lock was acquired")

        self.assertEqual("unsafe_lock", raised.exception.code)
        self.assertEqual(b"outside", external.read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows uses the child lock file handle")
    def test_preexisting_hardlink_is_rejected_without_writing_target(self) -> None:
        external = Path(self.temp.name) / "external.lock"
        external.write_bytes(b"outside")
        os.link(external, self.lock_path)

        with self.assertRaises(BallAnnotationServiceError) as raised:
            with _CoordinationLock(self.root):
                self.fail("unsafe lock was acquired")

        self.assertEqual("unsafe_lock", raised.exception.code)
        self.assertEqual(b"outside", external.read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows uses the child lock file handle")
    def test_preexisting_directory_reparse_is_rejected_without_touching_target(self) -> None:
        external = Path(self.temp.name) / "external-directory"
        external.mkdir()
        marker = external / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        _create_directory_link(self, self.lock_path, external)
        try:
            with self.assertRaises(BallAnnotationServiceError) as raised:
                with _CoordinationLock(self.root):
                    self.fail("unsafe lock was acquired")
            self.assertEqual("unsafe_lock", raised.exception.code)
            self.assertEqual("keep", marker.read_text(encoding="utf-8"))
        finally:
            _remove_directory_link(self.lock_path)

    @unittest.skipUnless(os.name == "nt", "Windows uses the child lock file handle")
    def test_replacement_immediately_before_open_is_rejected_without_writing_target(self) -> None:
        external = Path(self.temp.name) / "external.lock"
        external.write_bytes(b"outside")
        self.lock_path.write_bytes(b"")

        def replace_with_external_hardlink(path: Path) -> None:
            path.unlink()
            os.link(external, path)

        lock = _CoordinationLock(self.root, before_open_hook=replace_with_external_hardlink)
        with self.assertRaises(BallAnnotationServiceError) as raised:
            with lock:
                self.fail("replaced lock was acquired")

        self.assertEqual("unsafe_lock", raised.exception.code)
        self.assertEqual(b"outside", external.read_bytes())

    @unittest.skipIf(os.name == "nt", "POSIX-specific root directory handle identity")
    def test_posix_root_rename_after_open_fails_path_handle_identity(self) -> None:
        moved_root = self.root.with_name("moved-trusted")

        def replace_root_after_open(root: Path) -> None:
            root.rename(moved_root)
            root.mkdir()

        lock = _CoordinationLock(self.root, after_open_hook=replace_root_after_open)
        with self.assertRaises(BallAnnotationServiceError) as raised:
            with lock:
                self.fail("identity-swapped root lock was acquired")

        self.assertEqual("unsafe_lock", raised.exception.code)
        self.root.rmdir()
        moved_root.rename(self.root)

    @unittest.skipIf(os.name == "nt", "POSIX-specific no-follow root directory open")
    def test_posix_root_symlink_is_rejected(self) -> None:
        actual_root = self.root.with_name("actual-trusted")
        self.root.rename(actual_root)
        try:
            self.root.symlink_to(actual_root, target_is_directory=True)
        except OSError as exc:
            actual_root.rename(self.root)
            self.skipTest(f"directory symlinks are unavailable: {exc}")
        try:
            with self.assertRaises(BallAnnotationServiceError) as raised:
                with _CoordinationLock(self.root):
                    self.fail("symlinked root lock was acquired")
            self.assertEqual("unsafe_lock", raised.exception.code)
        finally:
            self.root.unlink()
            actual_root.rename(self.root)

    @unittest.skipUnless(os.name == "nt", "Windows-specific delete-sharing protection")
    def test_windows_verified_handle_prevents_replacement_after_open(self) -> None:
        self.lock_path.write_bytes(b"")
        replacement_was_denied = False

        def attempt_replacement(path: Path) -> None:
            nonlocal replacement_was_denied
            try:
                path.unlink()
            except PermissionError:
                replacement_was_denied = True

        with _CoordinationLock(self.root, after_open_hook=attempt_replacement):
            self.assertTrue(replacement_was_denied)

    @unittest.skipUnless(os.name == "nt", "Windows ctypes cache behavior only")
    def test_windows_coordination_lock_does_not_accumulate_ctypes_pointer_types(self) -> None:
        import ctypes

        with _CoordinationLock(self.root):
            pass
        pointer_types = set(ctypes._pointer_type_cache)

        for _ in range(100):
            with _CoordinationLock(self.root):
                pass

        self.assertEqual(pointer_types, set(ctypes._pointer_type_cache))

    def test_unlock_error_still_clears_thread_state_and_releases_thread_lock(self) -> None:
        lock = _CoordinationLock(self.root)
        with (
            patch.object(
                service_module,
                "_unlock_coordination_lock_handle",
                side_effect=OSError("injected unlock failure"),
            ),
            self.assertRaisesRegex(OSError, "injected unlock failure"),
        ):
            with lock:
                pass

        self.assertEqual(0, getattr(lock._local, "depth", 0))
        self.assertIsNone(lock._local.handle)
        self._assert_reacquirable_from_thread(lock)

    def test_close_error_still_clears_thread_state_and_releases_thread_lock(self) -> None:
        lock = _CoordinationLock(self.root)
        real_close = service_module._close_coordination_lock_handle

        def close_then_fail(handle: Any) -> None:
            real_close(handle)
            raise OSError("injected close failure")

        with (
            patch.object(
                service_module,
                "_close_coordination_lock_handle",
                side_effect=close_then_fail,
            ),
            self.assertRaisesRegex(OSError, "injected close failure"),
        ):
            with lock:
                pass

        self.assertEqual(0, getattr(lock._local, "depth", 0))
        self.assertIsNone(lock._local.handle)
        self._assert_reacquirable_from_thread(lock)

    def _assert_reacquirable_from_thread(self, lock: _CoordinationLock) -> None:
        acquired = threading.Event()
        errors: list[BaseException] = []

        def acquire() -> None:
            try:
                with lock:
                    acquired.set()
            except BaseException as exc:  # pragma: no cover - reported by the assertion below
                errors.append(exc)

        thread = threading.Thread(target=acquire, daemon=True)
        thread.start()
        self.assertTrue(acquired.wait(5), f"lock remained held after release failure: {errors}")
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)


class BallAnnotationStartupSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp.name) / "repo"
        (self.repo_root / "data").mkdir(parents=True)
        self.service = self._new_service()

    def tearDown(self) -> None:
        self.service.close()
        self.temp.cleanup()

    def _new_service(self) -> BallAnnotationService:
        return BallAnnotationService(
            self.repo_root,
            get_probe=_unexpected_probe,
            create_probe=_unexpected_probe,
            read_probe_artifact=_unexpected_probe,
        )

    @property
    def final_results_root(self) -> Path:
        return self.repo_root / "data" / "ball_detector_development_v1" / "annotation_sessions" / "final_results"

    @property
    def registry_path(self) -> Path:
        return (
            self.repo_root
            / "data"
            / "ball_detector_development_v1"
            / "annotation_sessions"
            / "temporal_group_registry.json"
        )

    def test_restart_removes_only_nonlink_rebuild_staging_directory(self) -> None:
        orphan = self.final_results_root / ".rebuild-session-interrupted"
        orphan.mkdir()
        (orphan / "partial.json").write_text("partial", encoding="utf-8")

        restarted = self._new_service()
        try:
            self.assertFalse(orphan.exists())
        finally:
            restarted.close()

    def test_restart_rejects_rebuild_symlink_without_touching_external_tree(self) -> None:
        external = Path(self.temp.name) / "external-result"
        external.mkdir()
        marker = external / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        link = self.final_results_root / ".rebuild-session-escape"
        _create_directory_link(self, link, external)
        try:
            with self.assertRaises(BallAnnotationServiceError) as raised:
                self._new_service()

            self.assertEqual("unsafe_final_result", raised.exception.code)
            self.assertEqual("keep", marker.read_text(encoding="utf-8"))
        finally:
            _remove_directory_link(link)

    def test_registry_canonical_and_persisted_caps_fail_before_replacement(self) -> None:
        registry = self.service._read_registry()
        prospective = dict(registry)
        prospective.pop("registry_sha256")
        prospective["registry_sha256"] = canonical_sha256(prospective)
        canonical_size = len(canonical_json_bytes(prospective))
        persisted_size = len(
            (json.dumps(prospective, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode(
                "utf-8"
            )
        )
        before = self.registry_path.read_bytes()

        for cap in (canonical_size - 1, persisted_size - 1):
            with self.subTest(cap=cap), patch.object(service_module, "_MAX_REGISTRY_BYTES", cap):
                with self.assertRaises(BallAnnotationServiceError) as raised:
                    self.service._write_registry(registry)
                self.assertEqual("resource_limit_exceeded", raised.exception.code)
                self.assertEqual(before, self.registry_path.read_bytes())

        restarted = self._new_service()
        try:
            self.assertEqual(registry, restarted._read_registry())
        finally:
            restarted.close()


if __name__ == "__main__":
    unittest.main()
