from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import football_tracking.detector_development_common as common
import football_tracking.detector_probe_worker as probe_worker
from football_tracking.detector_development_common import (
    DetectorDevelopmentError,
    atomic_write_json,
    hash_regular_file,
    read_regular_bytes,
)


class TrustedRegularFileReadTests(unittest.TestCase):
    @staticmethod
    def _sharing_error(winerror: int = 5) -> PermissionError:
        error = PermissionError("controlled Windows sharing collision")
        error.winerror = winerror
        return error

    @staticmethod
    def _current_thread_only(effect, fallback):
        owner = threading.get_ident()

        def scoped(*args, **kwargs):
            if threading.get_ident() == owner:
                return effect(*args, **kwargs)
            return fallback(*args, **kwargs)

        return scoped

    def _create_directory_link(self, link: Path, target: Path) -> None:
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                self.skipTest(f"directory junction unavailable: {completed.stderr or completed.stdout}")
            return
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")

    @staticmethod
    def _remove_directory_link(link: Path) -> None:
        if not os.path.lexists(link):
            return
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()

    def test_atomic_sibling_replacement_during_read_does_not_change_target_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "cancel.json"
            sibling = root / "heartbeat.json"
            replacement = root / ".heartbeat.next"
            content = b'{"cancel_requested":false}\n'
            target.write_bytes(content)
            sibling.write_text('{"sequence":0}\n', encoding="utf-8")
            replacement.write_text('{"sequence":1}\n', encoding="utf-8")
            original_snapshot = common.snapshot_identity_is_current
            replaced = False

            def replace_sibling(path: Path, expected: tuple[int, int, int, int, int]) -> bool:
                nonlocal replaced
                if path == target and not replaced:
                    replaced = True
                    os.replace(replacement, sibling)
                return original_snapshot(path, expected)

            with patch.object(
                common,
                "snapshot_identity_is_current",
                side_effect=replace_sibling,
            ):
                actual, digest = read_regular_bytes(
                    target,
                    "worker cancellation",
                    max_bytes=1024,
                    trusted_root=root,
                )

            self.assertTrue(replaced)
            self.assertEqual(content, actual)
            self.assertEqual(hashlib.sha256(content).hexdigest(), digest)
            self.assertEqual({"sequence": 1}, json.loads(sibling.read_text(encoding="utf-8")))

    def test_atomic_sibling_replacement_during_hash_does_not_change_target_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "input.json"
            sibling = root / "heartbeat.json"
            replacement = root / ".heartbeat.next"
            content = b'{"request":{}}\n'
            target.write_bytes(content)
            sibling.write_text('{"sequence":0}\n', encoding="utf-8")
            replacement.write_text('{"sequence":1}\n', encoding="utf-8")
            original_snapshot = common.snapshot_identity_is_current
            replaced = False

            def replace_sibling(path: Path, expected: tuple[int, int, int, int, int]) -> bool:
                nonlocal replaced
                if path == target and not replaced:
                    replaced = True
                    os.replace(replacement, sibling)
                return original_snapshot(path, expected)

            with patch.object(
                common,
                "snapshot_identity_is_current",
                side_effect=replace_sibling,
            ):
                digest, size = hash_regular_file(
                    target,
                    "worker input",
                    max_bytes=1024,
                    trusted_root=root,
                )

            self.assertTrue(replaced)
            self.assertEqual(hashlib.sha256(content).hexdigest(), digest)
            self.assertEqual(len(content), size)

    def test_actual_worker_heartbeat_can_publish_during_cancel_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            control = Path(temporary)
            worker_id = "worker-sibling-churn"
            atomic_write_json(
                control / "cancel.json",
                {
                    "schema_version": "1.0",
                    "artifact_type": "detector_probe_worker_cancel",
                    "worker_id": worker_id,
                    "cancel_requested": False,
                },
                trusted_root=control,
            )
            original_check = common._ancestor_identities_are_current
            expected_cancel_ancestors = common._capture_ancestor_identities(
                control / "cancel.json",
                control,
                "detector probe worker cancellation",
            )
            original_exit = probe_worker.os._exit
            forced_exit_codes: list[int] = []
            inside_heartbeat = False
            heartbeat_published = False
            heartbeat_writes = 0

            class OneHeartbeat:
                calls = 0

                def wait(self, _timeout: float) -> bool:
                    self.calls += 1
                    return self.calls > 1

            def publish_heartbeat_then_check(identities) -> bool:
                nonlocal inside_heartbeat, heartbeat_published, heartbeat_writes
                if identities != expected_cancel_ancestors or inside_heartbeat or heartbeat_published:
                    return original_check(identities)
                inside_heartbeat = True
                try:
                    probe_worker._heartbeat_loop(
                        OneHeartbeat(),
                        control,
                        worker_id,
                        os.getpid(),
                        lambda: True,
                    )
                    heartbeat_published = True
                    heartbeat_writes += 1
                finally:
                    inside_heartbeat = False
                return original_check(identities)

            scoped_exit = self._current_thread_only(
                forced_exit_codes.append,
                original_exit,
            )

            with (
                patch.object(
                    common,
                    "_ancestor_identities_are_current",
                    side_effect=publish_heartbeat_then_check,
                ),
                patch.object(probe_worker.os, "_exit", side_effect=scoped_exit),
            ):
                self.assertFalse(probe_worker._cancel_requested(control, worker_id))

            self.assertEqual([], forced_exit_codes)
            heartbeat = json.loads((control / "heartbeat.json").read_text(encoding="utf-8"))
            self.assertEqual(1, heartbeat_writes)
            self.assertEqual(worker_id, heartbeat["worker_id"])
            self.assertEqual(1, heartbeat["sequence"])

    def test_target_replacement_during_read_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "cancel.json"
            replacement = root / ".cancel.next"
            target.write_bytes(b'{"cancel_requested":false}\n')
            replacement.write_bytes(b'{"cancel_requested":true}\n')
            original_snapshot = common.snapshot_identity_is_current
            replaced = False

            def replace_target(path: Path, expected: tuple[int, int, int, int, int]) -> bool:
                nonlocal replaced
                if path == target and not replaced:
                    replaced = True
                    os.replace(replacement, target)
                return original_snapshot(path, expected)

            with (
                patch.object(
                    common,
                    "snapshot_identity_is_current",
                    side_effect=replace_target,
                ),
                self.assertRaises(DetectorDevelopmentError) as raised,
            ):
                read_regular_bytes(
                    target,
                    "worker cancellation",
                    max_bytes=1024,
                    trusted_root=root,
                )

            self.assertTrue(replaced)
            self.assertEqual("source_changed", raised.exception.code)

    def test_ancestor_directory_replacement_during_read_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "control"
            parent.mkdir()
            target = parent / "cancel.json"
            target.write_bytes(b'{"cancel_requested":false}\n')
            moved_parent = root / "control-old"
            original_snapshot = common.snapshot_identity_is_current
            replaced = False

            def replace_parent(path: Path, expected: tuple[int, int, int, int, int]) -> bool:
                nonlocal replaced
                if path != target:
                    return original_snapshot(path, expected)
                if path == target and not replaced:
                    replaced = True
                    parent.rename(moved_parent)
                    shutil.copytree(moved_parent, parent)
                return True

            with (
                patch.object(
                    common,
                    "snapshot_identity_is_current",
                    side_effect=replace_parent,
                ),
                self.assertRaises(DetectorDevelopmentError) as raised,
            ):
                read_regular_bytes(
                    target,
                    "worker cancellation",
                    max_bytes=1024,
                    trusted_root=root,
                )

            self.assertTrue(replaced)
            self.assertEqual("source_changed", raised.exception.code)

    def test_linked_ancestor_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            external = root / "external"
            external.mkdir()
            (external / "cancel.json").write_bytes(b'{"cancel_requested":false}\n')
            linked = root / "linked"
            self._create_directory_link(linked, external)
            try:
                with self.assertRaises(DetectorDevelopmentError) as raised:
                    read_regular_bytes(
                        linked / "cancel.json",
                        "worker cancellation",
                        max_bytes=1024,
                        trusted_root=root,
                    )
            finally:
                self._remove_directory_link(linked)

            self.assertEqual("unsafe_path", raised.exception.code)

    def test_ancestor_lstat_failure_during_recheck_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "control"
            parent.mkdir()
            target = parent / "cancel.json"
            target.write_bytes(b'{"cancel_requested":false}\n')
            original_lstat = Path.lstat
            original_snapshot = common.snapshot_identity_is_current
            reject_parent_lstat = False

            def controlled_lstat(path: Path):
                if reject_parent_lstat and path == parent:
                    raise PermissionError("controlled ancestor lstat failure")
                return original_lstat(path)

            def enable_failure(path: Path, expected: tuple[int, int, int, int, int]) -> bool:
                nonlocal reject_parent_lstat
                current = original_snapshot(path, expected)
                if path == target:
                    reject_parent_lstat = True
                return current

            with (
                patch.object(Path, "lstat", controlled_lstat),
                patch.object(
                    common,
                    "snapshot_identity_is_current",
                    side_effect=enable_failure,
                ),
                self.assertRaises(DetectorDevelopmentError) as raised,
            ):
                read_regular_bytes(
                    target,
                    "worker cancellation",
                    max_bytes=1024,
                    trusted_root=root,
                )

            self.assertEqual("source_changed", raised.exception.code)

    def test_ancestor_lstat_failure_during_capture_reports_path_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "control"
            parent.mkdir()
            target = parent / "cancel.json"
            target.write_bytes(b'{"cancel_requested":false}\n')
            original_lstat = Path.lstat

            def controlled_lstat(path: Path):
                if path == parent:
                    raise PermissionError("controlled ancestor lstat failure")
                return original_lstat(path)

            with (
                patch.object(Path, "lstat", controlled_lstat),
                self.assertRaises(DetectorDevelopmentError) as raised,
            ):
                read_regular_bytes(
                    target,
                    "worker cancellation",
                    max_bytes=1024,
                    trusted_root=root,
                )

            self.assertEqual("path_unavailable", raised.exception.code)

    def test_ancestor_disappearance_during_recheck_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "control"
            parent.mkdir()
            target = parent / "cancel.json"
            target.write_bytes(b'{"cancel_requested":false}\n')
            original_snapshot = common.snapshot_identity_is_current
            disappeared = False

            def remove_parent(path: Path, expected: tuple[int, int, int, int, int]) -> bool:
                nonlocal disappeared
                if path != target:
                    return original_snapshot(path, expected)
                if path == target and not disappeared:
                    disappeared = True
                    shutil.rmtree(parent)
                return True

            with (
                patch.object(
                    common,
                    "snapshot_identity_is_current",
                    side_effect=remove_parent,
                ),
                self.assertRaises(DetectorDevelopmentError) as raised,
            ):
                read_regular_bytes(
                    target,
                    "worker cancellation",
                    max_bytes=1024,
                    trusted_root=root,
                )

            self.assertTrue(disappeared)
            self.assertEqual("source_changed", raised.exception.code)

    def test_atomic_write_retries_bounded_windows_sharing_collisions(self) -> None:
        for winerror in (5, 32):
            with self.subTest(winerror=winerror), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                destination = root / "heartbeat.json"
                atomic_write_json(destination, {"sequence": 0}, trusted_root=root)
                original_replace = common.os.replace
                original_classifier = common._is_windows_atomic_replace_sharing_error
                original_sleep = common.time.sleep
                failure = self._sharing_error(winerror)
                attempts = 0
                bounded_sleeps: list[float] = []

                def flaky_replace(source: Path, target: Path) -> None:
                    nonlocal attempts
                    if Path(target) != destination:
                        original_replace(source, target)
                        return
                    attempts += 1
                    if attempts < 3:
                        raise failure
                    original_replace(source, target)

                def classify_sharing_error(exc: PermissionError) -> bool:
                    if exc is failure:
                        return True
                    return original_classifier(exc)

                scoped_sleep = self._current_thread_only(
                    bounded_sleeps.append,
                    original_sleep,
                )

                with (
                    patch.object(common.os, "replace", side_effect=flaky_replace),
                    patch.object(
                        common,
                        "_is_windows_atomic_replace_sharing_error",
                        side_effect=self._current_thread_only(
                            classify_sharing_error,
                            original_classifier,
                        ),
                        create=True,
                    ),
                    patch.object(common.time, "sleep", side_effect=scoped_sleep),
                ):
                    atomic_write_json(destination, {"sequence": 1}, trusted_root=root)

                self.assertEqual(3, attempts)
                self.assertEqual(2, len(bounded_sleeps))
                self.assertEqual(
                    {"sequence": 1},
                    json.loads(destination.read_text(encoding="utf-8")),
                )

    def test_atomic_write_sharing_classifier_is_windows_and_code_specific(self) -> None:
        expected_windows = os.name == "nt"
        self.assertEqual(
            expected_windows,
            common._is_windows_atomic_replace_sharing_error(self._sharing_error(5)),
        )
        self.assertEqual(
            expected_windows,
            common._is_windows_atomic_replace_sharing_error(self._sharing_error(32)),
        )
        self.assertFalse(common._is_windows_atomic_replace_sharing_error(self._sharing_error(33)))

    def test_atomic_write_persistent_sharing_collision_is_bounded_and_cleans_temp(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "heartbeat.json"
            atomic_write_json(destination, {"sequence": 0}, trusted_root=root)
            original_replace = common.os.replace
            original_classifier = common._is_windows_atomic_replace_sharing_error
            original_monotonic = common.time.monotonic
            failure = self._sharing_error()
            attempts = 0
            ticks = iter((100.0, 101.0))

            def locked_replace(source: Path, target: Path) -> None:
                nonlocal attempts
                if Path(target) != destination:
                    original_replace(source, target)
                    return
                attempts += 1
                raise failure

            def classify_sharing_error(exc: PermissionError) -> bool:
                if exc is failure:
                    return True
                return original_classifier(exc)

            scoped_monotonic = self._current_thread_only(
                lambda: next(ticks),
                original_monotonic,
            )

            with (
                patch.object(common.os, "replace", side_effect=locked_replace),
                patch.object(
                    common,
                    "_is_windows_atomic_replace_sharing_error",
                    side_effect=self._current_thread_only(
                        classify_sharing_error,
                        original_classifier,
                    ),
                    create=True,
                ),
                patch.object(common.time, "monotonic", side_effect=scoped_monotonic),
                self.assertRaises(PermissionError),
            ):
                atomic_write_json(destination, {"sequence": 1}, trusted_root=root)

            self.assertEqual(1, attempts)
            self.assertEqual([], list(root.glob(".heartbeat.json.*.tmp")))
            self.assertEqual(
                {"sequence": 0},
                json.loads(destination.read_text(encoding="utf-8")),
            )

    def test_atomic_write_does_not_attempt_after_retry_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "heartbeat.json"
            atomic_write_json(destination, {"sequence": 0}, trusted_root=root)
            original_replace = common.os.replace
            original_classifier = common._is_windows_atomic_replace_sharing_error
            original_monotonic = common.time.monotonic
            original_sleep = common.time.sleep
            failure = self._sharing_error()
            attempts = 0
            bounded_sleeps: list[float] = []
            ticks = iter((100.0, 100.5, 101.0))

            def locked_replace(source: Path, target: Path) -> None:
                nonlocal attempts
                if Path(target) != destination:
                    original_replace(source, target)
                    return
                attempts += 1
                raise failure

            def classify_sharing_error(exc: PermissionError) -> bool:
                if exc is failure:
                    return True
                return original_classifier(exc)

            scoped_monotonic = self._current_thread_only(
                lambda: next(ticks),
                original_monotonic,
            )
            scoped_sleep = self._current_thread_only(
                bounded_sleeps.append,
                original_sleep,
            )

            with (
                patch.object(common.os, "replace", side_effect=locked_replace),
                patch.object(
                    common,
                    "_is_windows_atomic_replace_sharing_error",
                    side_effect=self._current_thread_only(
                        classify_sharing_error,
                        original_classifier,
                    ),
                ),
                patch.object(common.time, "monotonic", side_effect=scoped_monotonic),
                patch.object(common.time, "sleep", side_effect=scoped_sleep),
                self.assertRaises(PermissionError),
            ):
                atomic_write_json(destination, {"sequence": 1}, trusted_root=root)

            self.assertEqual(1, attempts)
            self.assertEqual([0.005], bounded_sleeps)

    def test_atomic_write_does_not_retry_nonsharing_or_disk_full_errors(self) -> None:
        cases = (
            (PermissionError("permanent ACL denial"), False),
            (OSError(errno.ENOSPC, "controlled disk full"), False),
        )
        for failure, retryable in cases:
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                destination = root / "job.json"
                original_replace = common.os.replace
                original_classifier = common._is_windows_atomic_replace_sharing_error
                attempts = 0

                def failed_replace(source: Path, target: Path) -> None:
                    nonlocal attempts
                    if Path(target) != destination:
                        original_replace(source, target)
                        return
                    attempts += 1
                    raise failure

                def classify_sharing_error(exc: PermissionError) -> bool:
                    if exc is failure:
                        return retryable
                    return original_classifier(exc)

                with (
                    patch.object(common.os, "replace", side_effect=failed_replace),
                    patch.object(
                        common,
                        "_is_windows_atomic_replace_sharing_error",
                        side_effect=self._current_thread_only(
                            classify_sharing_error,
                            original_classifier,
                        ),
                        create=True,
                    ),
                    self.assertRaises(type(failure)),
                ):
                    atomic_write_json(destination, {"status": "ready"}, trusted_root=root)

                self.assertEqual(1, attempts)

    def test_atomic_write_revalidates_ancestor_before_sharing_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "heartbeat.json"
            atomic_write_json(destination, {"sequence": 0}, trusted_root=root)
            original_replace = common.os.replace
            original_classifier = common._is_windows_atomic_replace_sharing_error
            original_ancestor_check = common._ancestor_identities_are_current
            original_sleep = common.time.sleep
            expected_ancestors = common._capture_ancestor_identities(
                destination,
                root,
                "atomic JSON",
            )
            failure = self._sharing_error()
            checks = iter((True, False))
            attempts = 0
            bounded_sleeps: list[float] = []

            def locked_replace(source: Path, target: Path) -> None:
                nonlocal attempts
                if Path(target) != destination:
                    original_replace(source, target)
                    return
                attempts += 1
                raise failure

            def classify_sharing_error(exc: PermissionError) -> bool:
                if exc is failure:
                    return True
                return original_classifier(exc)

            def controlled_ancestor_check(identities) -> bool:
                if identities != expected_ancestors:
                    return original_ancestor_check(identities)
                return next(checks)

            scoped_sleep = self._current_thread_only(
                bounded_sleeps.append,
                original_sleep,
            )

            with (
                patch.object(common.os, "replace", side_effect=locked_replace),
                patch.object(
                    common,
                    "_is_windows_atomic_replace_sharing_error",
                    side_effect=self._current_thread_only(
                        classify_sharing_error,
                        original_classifier,
                    ),
                    create=True,
                ),
                patch.object(
                    common,
                    "_ancestor_identities_are_current",
                    side_effect=controlled_ancestor_check,
                ),
                patch.object(common.time, "sleep", side_effect=scoped_sleep),
                self.assertRaises(DetectorDevelopmentError) as raised,
            ):
                atomic_write_json(destination, {"sequence": 1}, trusted_root=root)

            self.assertEqual("source_changed", raised.exception.code)
            self.assertEqual(1, attempts)
            self.assertEqual([0.005], bounded_sleeps)

    def test_atomic_write_rejects_temporary_tamper_during_sharing_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "heartbeat.json"
            atomic_write_json(destination, {"sequence": 0}, trusted_root=root)
            original_replace = common.os.replace
            original_classifier = common._is_windows_atomic_replace_sharing_error
            original_sleep = common.time.sleep
            failure = self._sharing_error()
            attempts = 0
            bounded_sleeps: list[float] = []

            def tamper_then_lock(source: Path, target: Path) -> None:
                nonlocal attempts
                if Path(target) != destination:
                    original_replace(source, target)
                    return
                attempts += 1
                source.write_text('{"tampered":true}\n', encoding="utf-8")
                raise failure

            def classify_sharing_error(exc: PermissionError) -> bool:
                if exc is failure:
                    return True
                return original_classifier(exc)

            scoped_sleep = self._current_thread_only(
                bounded_sleeps.append,
                original_sleep,
            )

            with (
                patch.object(common.os, "replace", side_effect=tamper_then_lock),
                patch.object(
                    common,
                    "_is_windows_atomic_replace_sharing_error",
                    side_effect=self._current_thread_only(
                        classify_sharing_error,
                        original_classifier,
                    ),
                    create=True,
                ),
                patch.object(common.time, "sleep", side_effect=scoped_sleep),
                self.assertRaises(DetectorDevelopmentError) as raised,
            ):
                atomic_write_json(destination, {"sequence": 1}, trusted_root=root)

            self.assertEqual("source_changed", raised.exception.code)
            self.assertEqual(1, attempts)
            self.assertEqual([0.005], bounded_sleeps)

    def test_atomic_write_rejects_destination_tamper_during_sharing_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "heartbeat.json"
            atomic_write_json(destination, {"sequence": 0}, trusted_root=root)
            original_replace = common.os.replace
            original_classifier = common._is_windows_atomic_replace_sharing_error
            original_sleep = common.time.sleep
            failure = self._sharing_error()
            attempts = 0
            bounded_sleeps: list[float] = []

            def tamper_then_lock(source: Path, target: Path) -> None:
                nonlocal attempts
                if Path(target) != destination:
                    original_replace(source, target)
                    return
                attempts += 1
                target.write_text('{"sequence":999}\n', encoding="utf-8")
                raise failure

            def classify_sharing_error(exc: PermissionError) -> bool:
                if exc is failure:
                    return True
                return original_classifier(exc)

            scoped_sleep = self._current_thread_only(
                bounded_sleeps.append,
                original_sleep,
            )

            with (
                patch.object(common.os, "replace", side_effect=tamper_then_lock),
                patch.object(
                    common,
                    "_is_windows_atomic_replace_sharing_error",
                    side_effect=self._current_thread_only(
                        classify_sharing_error,
                        original_classifier,
                    ),
                ),
                patch.object(common.time, "sleep", side_effect=scoped_sleep),
                self.assertRaises(DetectorDevelopmentError) as raised,
            ):
                atomic_write_json(destination, {"sequence": 1}, trusted_root=root)

            self.assertEqual("source_changed", raised.exception.code)
            self.assertEqual(1, attempts)
            self.assertEqual([0.005], bounded_sleeps)

    def test_atomic_write_rejects_destination_reparse_during_sharing_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "heartbeat.json"
            external = root / "external"
            external.mkdir()
            original_replace = common.os.replace
            original_classifier = common._is_windows_atomic_replace_sharing_error
            original_sleep = common.time.sleep
            failure = self._sharing_error()
            attempts = 0
            bounded_sleeps: list[float] = []

            def link_then_lock(source: Path, target: Path) -> None:
                nonlocal attempts
                if Path(target) != destination:
                    original_replace(source, target)
                    return
                attempts += 1
                self._create_directory_link(target, external)
                raise failure

            def classify_sharing_error(exc: PermissionError) -> bool:
                if exc is failure:
                    return True
                return original_classifier(exc)

            scoped_sleep = self._current_thread_only(
                bounded_sleeps.append,
                original_sleep,
            )

            try:
                with (
                    patch.object(common.os, "replace", side_effect=link_then_lock),
                    patch.object(
                        common,
                        "_is_windows_atomic_replace_sharing_error",
                        side_effect=self._current_thread_only(
                            classify_sharing_error,
                            original_classifier,
                        ),
                    ),
                    patch.object(common.time, "sleep", side_effect=scoped_sleep),
                    self.assertRaises(DetectorDevelopmentError) as raised,
                ):
                    atomic_write_json(
                        destination,
                        {"sequence": 1},
                        trusted_root=root,
                    )
            finally:
                self._remove_directory_link(destination)

            self.assertEqual("unsafe_path", raised.exception.code)
            self.assertEqual(1, attempts)
            self.assertEqual([0.005], bounded_sleeps)

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics only")
    def test_atomic_heartbeat_write_survives_concurrent_same_path_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            heartbeat = root / "heartbeat.json"
            atomic_write_json(heartbeat, {"sequence": 0}, trusted_root=root)
            stop = threading.Event()
            unexpected: list[BaseException] = []

            def read_heartbeat() -> None:
                while not stop.is_set():
                    try:
                        read_regular_bytes(
                            heartbeat,
                            "detector probe worker heartbeat",
                            max_bytes=4096,
                            trusted_root=root,
                        )
                    except DetectorDevelopmentError as exc:
                        if exc.code not in {"source_changed", "path_unavailable"}:
                            unexpected.append(exc)
                            return
                    except BaseException as exc:
                        unexpected.append(exc)
                        return

            reader = threading.Thread(target=read_heartbeat, daemon=True)
            reader.start()
            try:
                for sequence in range(1, 501):
                    atomic_write_json(
                        heartbeat,
                        {"sequence": sequence},
                        trusted_root=root,
                    )
            finally:
                stop.set()
                reader.join(timeout=5.0)

            self.assertFalse(reader.is_alive())
            self.assertEqual([], unexpected)
            self.assertEqual(
                {"sequence": 500},
                json.loads(heartbeat.read_text(encoding="utf-8")),
            )


if __name__ == "__main__":
    unittest.main()
