from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
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
    def _create_directory_link(self, link: Path, target: Path) -> None:
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                self.skipTest(
                    f"directory junction unavailable: {completed.stderr or completed.stdout}"
                )
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
                if not replaced:
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
                if not replaced:
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
            inside_heartbeat = False
            heartbeat_writes = 0

            class OneHeartbeat:
                calls = 0

                def wait(self, _timeout: float) -> bool:
                    self.calls += 1
                    return self.calls > 1

            def publish_heartbeat_then_check(identities) -> bool:
                nonlocal inside_heartbeat, heartbeat_writes
                if inside_heartbeat:
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
                    heartbeat_writes += 1
                finally:
                    inside_heartbeat = False
                return original_check(identities)

            with (
                patch.object(
                    common,
                    "_ancestor_identities_are_current",
                    side_effect=publish_heartbeat_then_check,
                ),
                patch.object(probe_worker.os, "_exit") as forced_exit,
            ):
                self.assertFalse(probe_worker._cancel_requested(control, worker_id))

            forced_exit.assert_not_called()
            heartbeat = json.loads(
                (control / "heartbeat.json").read_text(encoding="utf-8")
            )
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
                if not replaced:
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
            replaced = False

            def replace_parent(_path: Path, _expected: tuple[int, int, int, int, int]) -> bool:
                nonlocal replaced
                if not replaced:
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

            def enable_failure(
                path: Path, expected: tuple[int, int, int, int, int]
            ) -> bool:
                nonlocal reject_parent_lstat
                current = original_snapshot(path, expected)
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
            disappeared = False

            def remove_parent(
                _path: Path, _expected: tuple[int, int, int, int, int]
            ) -> bool:
                nonlocal disappeared
                if not disappeared:
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


if __name__ == "__main__":
    unittest.main()
