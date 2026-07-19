from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import football_tracking.detector_development_common as common
from football_tracking.detector_development_common import (
    DetectorDevelopmentError,
    exact_regular_tree_snapshot,
    hash_regular_file,
    read_regular_bytes,
    regular_file_change_identity,
    stat_token,
)


@unittest.skipUnless(os.name == "nt", "NTFS ChangeTime attacks are Windows-specific")
class WindowsChangeIdentityAttackTests(unittest.TestCase):
    @staticmethod
    def _rewrite_and_restore_mtime(path: Path, content: bytes) -> None:
        before = path.stat()
        path.write_bytes(content)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    def test_repeated_secure_reads_reuse_windows_ctypes_pointer_types(self) -> None:
        import ctypes

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "heartbeat.json"
            target.write_text('{"sequence":1}\n', encoding="utf-8")
            directory_identity = stat_token(root.stat())
            read_regular_bytes(
                target,
                "worker heartbeat",
                max_bytes=1024,
                trusted_root=root,
            )
            common._windows_directory_change_time(
                root,
                directory_identity,
                "worker control directory",
            )
            api = common._windows_file_api()
            pointer_types = set(ctypes._pointer_type_cache)

            for _ in range(100):
                read_regular_bytes(
                    target,
                    "worker heartbeat",
                    max_bytes=1024,
                    trusted_root=root,
                )
                common._windows_directory_change_time(
                    root,
                    directory_identity,
                    "worker control directory",
                )

            self.assertIs(api, common._windows_file_api())
            self.assertEqual(pointer_types, set(ctypes._pointer_type_cache))

    def test_read_rejects_same_size_rewrite_with_restored_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "result.json"
            target.write_bytes(b'{"value":"original"}\n')
            before_stat = stat_token(target.stat())
            before_change = regular_file_change_identity(target, "sealed result")
            original_snapshot = common.snapshot_identity_is_current
            attacked = False

            def rewrite_then_check(path: Path, expected: tuple[int, int, int, int, int]) -> bool:
                nonlocal attacked
                if path == target and not attacked:
                    attacked = True
                    self._rewrite_and_restore_mtime(path, b'{"value":"tampered"}\n')
                return original_snapshot(path, expected)

            with (
                patch.object(
                    common,
                    "snapshot_identity_is_current",
                    side_effect=rewrite_then_check,
                ),
                self.assertRaises(DetectorDevelopmentError) as raised,
            ):
                read_regular_bytes(
                    target,
                    "sealed result",
                    max_bytes=1024,
                    trusted_root=root,
                )

            self.assertTrue(attacked)
            self.assertEqual(before_stat, stat_token(target.stat()))
            self.assertEqual(
                before_change,
                regular_file_change_identity(target, "sealed result"),
            )
            self.assertEqual(b'{"value":"original"}\n', target.read_bytes())
            self.assertEqual("source_changed", raised.exception.code)
            self.assertEqual("sealed result changed while it was read", str(raised.exception))

    def test_hash_rejects_same_size_rewrite_with_restored_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "payload.bin"
            target.write_bytes(b"A" * 64)
            before_stat = stat_token(target.stat())
            original_snapshot = common.snapshot_identity_is_current
            attacked = False

            def rewrite_then_check(path: Path, expected: tuple[int, int, int, int, int]) -> bool:
                nonlocal attacked
                if path == target and not attacked:
                    attacked = True
                    self._rewrite_and_restore_mtime(path, b"B" * 64)
                return original_snapshot(path, expected)

            with (
                patch.object(
                    common,
                    "snapshot_identity_is_current",
                    side_effect=rewrite_then_check,
                ),
                self.assertRaises(DetectorDevelopmentError) as raised,
            ):
                hash_regular_file(
                    target,
                    "sealed payload",
                    max_bytes=1024,
                    trusted_root=root,
                )

            self.assertTrue(attacked)
            self.assertEqual(before_stat, stat_token(target.stat()))
            self.assertEqual("source_changed", raised.exception.code)

    def test_read_and_hash_reject_child_path_swap_and_restore(self) -> None:
        for operation in ("read", "hash"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                target = root / "payload.bin"
                target.write_bytes(b"original")
                replacement = root / "replacement.bin"
                replacement.write_bytes(b"tampered")
                original_away = root / "original-away.bin"
                replacement_away = root / "replacement-away.bin"
                before_stat = stat_token(target.stat())
                original_snapshot = common.snapshot_identity_is_current
                attacked = False

                def swap_restore_then_check(path: Path, expected: tuple[int, int, int, int, int]) -> bool:
                    nonlocal attacked
                    if path == target and not attacked:
                        attacked = True
                        target.rename(original_away)
                        replacement.rename(target)
                        target.rename(replacement_away)
                        original_away.rename(target)
                    return original_snapshot(path, expected)

                with (
                    patch.object(
                        common,
                        "snapshot_identity_is_current",
                        side_effect=swap_restore_then_check,
                    ),
                    self.assertRaises(DetectorDevelopmentError) as raised,
                ):
                    if operation == "read":
                        read_regular_bytes(
                            target,
                            "sealed payload",
                            max_bytes=1024,
                            trusted_root=root,
                        )
                    else:
                        hash_regular_file(
                            target,
                            "sealed payload",
                            max_bytes=1024,
                            trusted_root=root,
                        )

                self.assertTrue(attacked)
                self.assertEqual(before_stat, stat_token(target.stat()))
                self.assertEqual(b"original", target.read_bytes())
                self.assertEqual("source_changed", raised.exception.code)

    def test_exact_tree_rejects_file_rewrite_between_node_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trusted_root = Path(temporary)
            tree = trusted_root / "result"
            tree.mkdir()
            payload = tree / "payload.bin"
            payload.write_bytes(b"A" * 64)
            before_stat = stat_token(payload.stat())
            original_ancestor_check = common._ancestor_identities_are_current
            expected_ancestors = common._capture_ancestor_identities(
                tree,
                trusted_root,
                "sealed tree",
            )
            attacked = False

            def rewrite_after_capture(identities) -> bool:
                nonlocal attacked
                if identities == expected_ancestors and not attacked:
                    attacked = True
                    self._rewrite_and_restore_mtime(payload, b"B" * 64)
                return original_ancestor_check(identities)

            with (
                patch.object(
                    common,
                    "_ancestor_identities_are_current",
                    side_effect=rewrite_after_capture,
                ),
                self.assertRaises(DetectorDevelopmentError) as raised,
            ):
                exact_regular_tree_snapshot(
                    tree,
                    {"payload.bin"},
                    "sealed tree",
                    trusted_root=trusted_root,
                )

            self.assertTrue(attacked)
            self.assertEqual(before_stat, stat_token(payload.stat()))
            self.assertEqual(b"A" * 64, payload.read_bytes())
            self.assertEqual("source_changed", raised.exception.code)

    def test_exact_tree_holds_all_file_guards_through_final_validation(self) -> None:
        for attack_kind in ("rewrite", "replace"):
            with self.subTest(attack_kind=attack_kind), tempfile.TemporaryDirectory() as temporary:
                trusted_root = Path(temporary)
                tree = trusted_root / "result"
                tree.mkdir()
                first = tree / "a.bin"
                second = tree / "b.bin"
                first.write_bytes(b"A")
                second.write_bytes(b"B")
                replacement = trusted_root / "replacement.bin"
                replacement.write_bytes(b"X")
                original_change_time = common._windows_file_change_time
                second_calls = 0
                attempted = False
                blocked = False

                def attack_before_second_final_check(path: Path, expected, label):
                    nonlocal second_calls, attempted, blocked
                    if path == second:
                        second_calls += 1
                        if second_calls == 2:
                            attempted = True
                            try:
                                if attack_kind == "rewrite":
                                    first.write_bytes(b"X")
                                else:
                                    replacement.replace(first)
                            except PermissionError:
                                blocked = True
                    return original_change_time(path, expected, label)

                with patch.object(
                    common,
                    "_windows_file_change_time",
                    side_effect=attack_before_second_final_check,
                ):
                    snapshot = exact_regular_tree_snapshot(
                        tree,
                        {"a.bin", "b.bin"},
                        "sealed tree",
                        trusted_root=trusted_root,
                    )

                self.assertTrue(attempted)
                self.assertTrue(blocked)
                self.assertEqual(b"A", first.read_bytes())
                self.assertEqual(
                    snapshot,
                    exact_regular_tree_snapshot(
                        tree,
                        {"a.bin", "b.bin"},
                        "sealed tree",
                        trusted_root=trusted_root,
                    ),
                )

    def test_exact_tree_reenumeration_rejects_late_unexpected_entries(self) -> None:
        for entry_kind in ("file", "directory"):
            with self.subTest(entry_kind=entry_kind), tempfile.TemporaryDirectory() as temporary:
                trusted_root = Path(temporary)
                tree = trusted_root / "result"
                tree.mkdir()
                expected_file = tree / "a.bin"
                expected_file.write_bytes(b"A")
                original_change_time = common._windows_file_change_time
                expected_calls = 0
                attacked = False

                def create_before_final_file_check(path: Path, expected, label):
                    nonlocal expected_calls, attacked
                    if path == expected_file:
                        expected_calls += 1
                        if expected_calls == 2:
                            attacked = True
                            unexpected = tree / "unexpected"
                            if entry_kind == "file":
                                unexpected.write_bytes(b"X")
                            else:
                                unexpected.mkdir()
                    return original_change_time(path, expected, label)

                with (
                    patch.object(
                        common,
                        "_windows_file_change_time",
                        side_effect=create_before_final_file_check,
                    ),
                    self.assertRaises(DetectorDevelopmentError) as raised,
                ):
                    exact_regular_tree_snapshot(
                        tree,
                        {"a.bin"},
                        "sealed tree",
                        trusted_root=trusted_root,
                    )

                self.assertTrue(attacked)
                self.assertEqual(
                    "unexpected_result_artifact",
                    raised.exception.code,
                )

    def test_exact_tree_guard_acquisition_failure_releases_prior_handles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trusted_root = Path(temporary)
            tree = trusted_root / "result"
            tree.mkdir()
            (tree / "a.bin").write_bytes(b"A")
            (tree / "b.bin").write_bytes(b"B")
            original_open = common._open_verified_regular_file
            opened = []
            guarded_paths = {tree / "a.bin", tree / "b.bin"}

            def fail_second_open(path: Path, expected, label):
                if path not in guarded_paths:
                    return original_open(path, expected, label)
                if opened:
                    raise DetectorDevelopmentError(
                        "source_changed",
                        "second guard acquisition failed",
                    )
                guard = original_open(path, expected, label)
                opened.append(guard)
                return guard

            with (
                patch.object(
                    common,
                    "_open_verified_regular_file",
                    side_effect=fail_second_open,
                ),
                self.assertRaises(DetectorDevelopmentError),
            ):
                exact_regular_tree_snapshot(
                    tree,
                    {"a.bin", "b.bin"},
                    "sealed tree",
                    trusted_root=trusted_root,
                )

            self.assertEqual(1, len(opened))
            self.assertTrue(opened[0].closed)
            renamed = trusted_root / "renamed"
            tree.rename(renamed)
            self.assertTrue(renamed.is_dir())

    def test_exact_tree_rejects_directory_swap_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trusted_root = Path(temporary)
            tree = trusted_root / "result"
            tree.mkdir()
            (tree / "payload.bin").write_bytes(b"original")
            replacement = trusted_root / "replacement"
            replacement.mkdir()
            (replacement / "payload.bin").write_bytes(b"tampered")
            original_away = trusted_root / "original-away"
            replacement_away = trusted_root / "replacement-away"
            before = tree.stat()
            before_stat = stat_token(before)
            original_ancestor_check = common._ancestor_identities_are_current
            expected_ancestors = common._capture_ancestor_identities(
                tree,
                trusted_root,
                "sealed tree",
            )
            attacked = False

            def swap_and_restore(identities) -> bool:
                nonlocal attacked
                if identities == expected_ancestors and not attacked:
                    attacked = True
                    tree.rename(original_away)
                    replacement.rename(tree)
                    tree.rename(replacement_away)
                    original_away.rename(tree)
                    os.utime(tree, ns=(before.st_atime_ns, before.st_mtime_ns))
                return original_ancestor_check(identities)

            with (
                patch.object(
                    common,
                    "_ancestor_identities_are_current",
                    side_effect=swap_and_restore,
                ),
                self.assertRaises(DetectorDevelopmentError) as raised,
            ):
                exact_regular_tree_snapshot(
                    tree,
                    {"payload.bin"},
                    "sealed tree",
                    trusted_root=trusted_root,
                )

            self.assertTrue(attacked)
            self.assertEqual(before_stat, stat_token(tree.stat()))
            self.assertEqual(b"original", (tree / "payload.bin").read_bytes())
            self.assertEqual("source_changed", raised.exception.code)

    def test_read_guard_blocks_ancestor_directory_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trusted_root = Path(temporary)
            parent = trusted_root / "control"
            parent.mkdir()
            target = parent / "cancel.json"
            content = b'{"cancel_requested":false}\n'
            target.write_bytes(content)
            original_snapshot = common.snapshot_identity_is_current
            blocked = False

            def attempt_ancestor_rename(path: Path, expected: tuple[int, int, int, int, int]) -> bool:
                nonlocal blocked
                if path != target:
                    return original_snapshot(path, expected)
                try:
                    parent.rename(trusted_root / "control-away")
                except PermissionError:
                    blocked = True
                return original_snapshot(path, expected)

            with patch.object(
                common,
                "snapshot_identity_is_current",
                side_effect=attempt_ancestor_rename,
            ):
                observed, _digest = read_regular_bytes(
                    target,
                    "worker cancellation",
                    max_bytes=1024,
                    trusted_root=trusted_root,
                )

            self.assertTrue(blocked)
            self.assertEqual(content, observed)

    def test_verified_target_handle_blocks_rename_until_read_closes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "payload.bin"
            content = b"sealed-content"
            target.write_bytes(content)
            moved = root / "payload-away.bin"
            expected = regular_file_change_identity(target, "sealed payload")

            with common._open_verified_regular_file(
                target,
                expected,
                "sealed payload",
            ) as handle:
                with self.assertRaises(PermissionError):
                    target.rename(moved)
                with self.assertRaises(PermissionError):
                    target.write_bytes(b"tampered-data")
                self.assertEqual(content, handle.read())

            target.rename(moved)
            self.assertEqual(content, moved.read_bytes())

    def test_directory_change_time_binds_the_open_handle_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trusted_root = Path(temporary)
            directory = trusted_root / "result"
            directory.mkdir()
            original_away = trusted_root / "original-away"
            replacement = trusted_root / "replacement"
            replacement.mkdir()
            replacement_away = trusted_root / "replacement-away"
            expected = stat_token(directory.stat())
            directory.rename(original_away)
            replacement.rename(directory)
            original_stat = Path.stat
            restored_during_path_recheck = False

            def restore_before_path_stat(path: Path, *args, **kwargs):
                nonlocal restored_during_path_recheck
                if path == directory and not restored_during_path_recheck:
                    restored_during_path_recheck = True
                    directory.rename(replacement_away)
                    original_away.rename(directory)
                return original_stat(path, *args, **kwargs)

            try:
                with (
                    patch.object(Path, "stat", restore_before_path_stat),
                    self.assertRaises(DetectorDevelopmentError) as raised,
                ):
                    common._windows_directory_change_time(
                        directory,
                        expected,
                        "sealed directory",
                    )
            finally:
                if original_away.exists():
                    if directory.exists():
                        directory.rename(replacement_away)
                    original_away.rename(directory)

            self.assertFalse(restored_during_path_recheck)
            self.assertEqual("source_changed", raised.exception.code)

    def test_file_change_time_blocks_rewrite_after_handle_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "payload.bin"
            original = b"original"
            target.write_bytes(original)
            before = target.stat()
            expected = stat_token(before)
            expected_change_time = common._windows_file_change_time(
                target,
                expected,
                "sealed payload",
            )
            original_stat = Path.stat
            attempted = False
            blocked = False

            def attack_after_handle_sampling(path: Path, *args, **kwargs):
                nonlocal attempted, blocked
                if path == target and not attempted:
                    attempted = True
                    try:
                        target.write_bytes(b"tampered")
                        os.utime(
                            target,
                            ns=(before.st_atime_ns, before.st_mtime_ns),
                        )
                    except PermissionError:
                        blocked = True
                return original_stat(path, *args, **kwargs)

            with patch.object(Path, "stat", attack_after_handle_sampling):
                observed_change_time = common._windows_file_change_time(
                    target,
                    expected,
                    "sealed payload",
                )

            self.assertTrue(attempted)
            self.assertTrue(blocked)
            self.assertEqual(expected_change_time, observed_change_time)
            self.assertEqual(expected, stat_token(target.stat()))
            self.assertEqual(original, target.read_bytes())

    def test_directory_change_time_blocks_swap_after_handle_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "result"
            directory.mkdir()
            (directory / "payload.bin").write_bytes(b"original")
            replacement = root / "replacement"
            replacement.mkdir()
            (replacement / "payload.bin").write_bytes(b"tampered")
            original_away = root / "original-away"
            replacement_away = root / "replacement-away"
            before = directory.stat()
            expected = stat_token(before)
            expected_change_time = common._windows_directory_change_time(
                directory,
                expected,
                "sealed directory",
            )
            original_stat = Path.stat
            attempted = False
            blocked = False

            def attack_after_handle_sampling(path: Path, *args, **kwargs):
                nonlocal attempted, blocked
                if path == directory and not attempted:
                    attempted = True
                    try:
                        directory.rename(original_away)
                        replacement.rename(directory)
                        directory.rename(replacement_away)
                        original_away.rename(directory)
                        os.utime(
                            directory,
                            ns=(before.st_atime_ns, before.st_mtime_ns),
                        )
                    except PermissionError:
                        blocked = True
                return original_stat(path, *args, **kwargs)

            observed_change_time = None
            try:
                with patch.object(Path, "stat", attack_after_handle_sampling):
                    observed_change_time = common._windows_directory_change_time(
                        directory,
                        expected,
                        "sealed directory",
                    )
            finally:
                if original_away.exists():
                    if directory.exists():
                        directory.rename(replacement_away)
                    original_away.rename(directory)

            self.assertTrue(attempted)
            self.assertTrue(blocked)
            self.assertEqual(expected_change_time, observed_change_time)
            self.assertEqual(expected, stat_token(directory.stat()))

    def test_directory_change_time_captures_entry_write_after_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "result"
            directory.mkdir()
            before = directory.stat()
            expected = stat_token(before)
            expected_change_time = common._windows_directory_change_time(
                directory,
                expected,
                "sealed directory",
            )
            original_stat = Path.stat
            attempted = False

            def attack_after_handle_sampling(path: Path, *args, **kwargs):
                nonlocal attempted
                if path == directory and not attempted:
                    attempted = True
                    transient = directory / "transient-entry"
                    transient.write_bytes(b"changed")
                    transient.unlink()
                    time.sleep(0.02)
                    os.utime(
                        directory,
                        ns=(before.st_atime_ns, before.st_mtime_ns),
                    )
                    time.sleep(0.02)
                return original_stat(path, *args, **kwargs)

            with patch.object(Path, "stat", attack_after_handle_sampling):
                observed_change_time = common._windows_directory_change_time(
                    directory,
                    expected,
                    "sealed directory",
                )

            self.assertTrue(attempted)
            self.assertNotEqual(expected_change_time, observed_change_time)
            self.assertEqual(
                observed_change_time,
                common._windows_directory_change_time(
                    directory,
                    expected,
                    "sealed directory",
                ),
            )
            self.assertEqual(expected, stat_token(directory.stat()))


if __name__ == "__main__":
    unittest.main()
