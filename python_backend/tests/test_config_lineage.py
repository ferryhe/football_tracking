from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import football_tracking.config_lineage as config_lineage_module
from football_tracking.config_lineage import (
    CONFIG_LINEAGE_MISMATCH,
    CONFIG_LINEAGE_UNSAFE,
    ConfigLineageError,
    inspect_config_bytes,
    load_config_lineage_reconfirmation,
    reconfirm_config_lineage,
)


def _bindings() -> dict[str, object]:
    return {
        "workflow_id": "workflow-1",
        "accepted_trial": {
            "run_id": "trial-1",
            "record_sha256": "1" * 64,
            "notes_sha256": "a" * 64,
        },
        "request": {"sha256": "2" * 64},
        "intent": {"sha256": "3" * 64},
        "trial_patch": {"sha256": "4" * 64},
        "production_patch": {"sha256": "b" * 64},
        "calibration": {"sha256": "5" * 64},
        "source_signature": {"sha256": "6" * 64},
        "historical_full_runs": [
            {
                "run_id": "production-full-failed",
                "submission_id": "submission-1",
                "generation_id": "generation-1",
                "status": "failed",
                "record_sha256": "7" * 64,
                "notes_sha256": "8" * 64,
            },
            {
                "run_id": "production-full-completed",
                "submission_id": "submission-2",
                "generation_id": "generation-2",
                "status": "completed",
                "record_sha256": "9" * 64,
                "notes_sha256": "c" * 64,
            },
        ],
    }


class ConfigCanonicalizationTests(unittest.TestCase):
    def test_workflow_bindings_serialization_failures_are_typed_mismatches(self) -> None:
        for invalid_value in ({"not-json"}, None):
            with self.subTest(kind="circular" if invalid_value is None else "non-serializable"):
                bindings = _bindings()
                if invalid_value is None:
                    bindings["request"] = bindings
                else:
                    bindings["request"] = invalid_value
                with self.assertRaises(ConfigLineageError) as caught:
                    config_lineage_module._validated_workflow_bindings(bindings)
                self.assertEqual(CONFIG_LINEAGE_MISMATCH, caught.exception.code)
                self.assertEqual(
                    "config lineage snapshot mismatch: workflow bindings must be JSON-serializable",
                    str(caught.exception),
                )

    def test_workflow_binding_serialization_preserves_domain_errors(self) -> None:
        unsafe_error = ConfigLineageError(CONFIG_LINEAGE_UNSAFE, "unsafe-domain")

        class HostileDict(dict[str, object]):
            def items(self):
                raise unsafe_error

        class HostileList(list[object]):
            def __iter__(self):
                raise unsafe_error

        for kind, hostile_value in (
            ("dict", HostileDict({"value": 1})),
            ("list", HostileList([1])),
        ):
            with self.subTest(kind=kind):
                bindings = _bindings()
                bindings["request"] = {"nested": hostile_value}
                with self.assertRaises(ConfigLineageError) as caught:
                    config_lineage_module._validated_workflow_bindings(bindings)
                self.assertIs(unsafe_error, caught.exception)
                self.assertEqual(CONFIG_LINEAGE_UNSAFE, caught.exception.code)
                self.assertEqual("unsafe-domain", str(caught.exception))

    def test_crlf_and_lf_have_same_canonical_digest(self) -> None:
        crlf = inspect_config_bytes(b"first: 1\r\nsecond: 2\r\n")
        lf = inspect_config_bytes(b"first: 1\nsecond: 2\n")
        self.assertNotEqual(crlf.observed_raw_sha256, lf.observed_raw_sha256)
        self.assertEqual(crlf.confirmed_text_sha256, lf.confirmed_text_sha256)
        self.assertEqual(b"first: 1\nsecond: 2\n", crlf.canonical_bytes)
        self.assertEqual(2, crlf.crlf_count)
        self.assertEqual(0, crlf.lf_count)

    def test_bom_mixed_newlines_and_invalid_utf8_are_rejected(self) -> None:
        for raw in (
            b"\xef\xbb\xbffirst: 1\n",
            b"first: 1\r\nsecond: 2\n",
            b"first: \xff\n",
            b"first: 1\rsecond: 2\r",
        ):
            with self.subTest(raw=raw), self.assertRaises(ConfigLineageError):
                inspect_config_bytes(raw)

    @unittest.skipUnless(os.name == "nt", "Windows-only fail-closed capability contract")
    def test_windows_lineage_publication_fails_closed_without_native_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_root = root / "config"
            config_root.mkdir()
            config_path = config_root / "confirmed.yaml"
            config_path.write_bytes(b"first: 1\r\n")
            inspection = inspect_config_bytes(config_path.read_bytes())
            with self.assertRaises(ConfigLineageError) as caught:
                reconfirm_config_lineage(
                    trusted_config_root=config_root,
                    observed_config_path=config_path,
                    lineage_root=root / "lineage",
                    target_run_id="run-1",
                    confirmed_config_name=config_path.name,
                    confirmed_text_sha256=inspection.confirmed_text_sha256,
                    expected_observed_raw_sha256=inspection.observed_raw_sha256,
                    workflow_bindings=_bindings(),
                    operator_id="operator-1",
                    reviewer_id="reviewer-1",
                )
        self.assertEqual(CONFIG_LINEAGE_UNSAFE, caught.exception.code)
        self.assertIn("handle-relative", str(caught.exception))

    def test_lineage_module_has_no_path_reopen_or_legacy_fallback(self) -> None:
        source = Path(config_lineage_module.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "_capture_stable_regular_file",
            "_open_file_no_follow",
            "_visible_generations",
            "_write_file_exclusive",
            "_publish_directory_noreplace",
            "_exclusive_file_lock",
            "_ensure_managed_directory",
            ".lstat(",
            ".iterdir(",
            "shutil.rmtree",
            "CreateFileW",
            "MoveFileExW",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        self.assertIn("dir_fd=", source)
        self.assertIn("os.O_NOFOLLOW", source)
        service_source = (
            Path(config_lineage_module.__file__).parent / "api" / "service.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("sha256_file(generation.manifest_path)", service_source)
        self.assertNotIn(
            "sha256_file(generation.canonical_snapshot_path)",
            service_source,
        )


@unittest.skipIf(os.name == "nt", "Windows native handle-relative backend intentionally fails closed")
class ConfigLineagePublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_root = self.root / "config"
        self.lineage_root = self.root / "outputs" / "config_lineage_reconfirmations"
        self.config_root.mkdir()
        self.config_path = self.config_root / "confirmed.yaml"
        self.config_path.write_bytes(b"first: 1\r\nsecond: 2\r\n")
        self.inspection = inspect_config_bytes(self.config_path.read_bytes())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _publish(self, **overrides: object):
        values = {
            "trusted_config_root": self.config_root,
            "observed_config_path": self.config_path,
            "lineage_root": self.lineage_root,
            "target_run_id": "run-1",
            "confirmed_config_name": "confirmed.yaml",
            "confirmed_text_sha256": self.inspection.confirmed_text_sha256,
            "expected_observed_raw_sha256": self.inspection.observed_raw_sha256,
            "workflow_bindings": _bindings(),
            "operator_id": "operator-1",
            "reviewer_id": "reviewer-1",
        }
        values.update(overrides)
        return reconfirm_config_lineage(**values)

    def test_owned_directory_metadata_changes_do_not_change_its_identity(self) -> None:
        probe = self.root / "identity-probe"
        with config_lineage_module._open_absolute_directory(
            self.root,
            create=False,
        ) as directory:
            directory.write_exclusive(probe.name, b"ok\n")
            directory.assert_current()
        self.assertEqual(b"ok\n", probe.read_bytes())

    def test_lock_body_exceptions_propagate_and_the_lock_is_released(self) -> None:
        import fcntl

        with config_lineage_module._open_absolute_directory(
            self.root,
            create=False,
        ) as directory:
            for index, sentinel in enumerate(
                (OSError("body-oserror"), RuntimeError("body-runtime")),
            ):
                with self.subTest(error=type(sentinel).__name__):
                    lock_name = f".body-exception-{index}.lock"
                    with self.assertRaises(type(sentinel)) as caught:
                        with directory.lock(lock_name):
                            raise sentinel
                    self.assertIs(sentinel, caught.exception)

                    descriptor = os.open(self.root / lock_name, os.O_RDWR)
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    finally:
                        os.close(descriptor)

    def test_lock_setup_and_release_oserrors_remain_typed_unsafe(self) -> None:
        import fcntl

        with config_lineage_module._open_absolute_directory(
            self.root,
            create=False,
        ) as directory:
            with (
                patch.object(fcntl, "flock", side_effect=OSError("setup failure")),
                self.assertRaises(ConfigLineageError) as setup_caught,
            ):
                with directory.lock(".setup-failure.lock"):
                    self.fail("lock body must not run")
            self.assertEqual(CONFIG_LINEAGE_UNSAFE, setup_caught.exception.code)
            self.assertIn("anchored lock failed", str(setup_caught.exception))

            original_flock = fcntl.flock

            def fail_release(descriptor, operation):
                if operation == fcntl.LOCK_UN:
                    raise OSError("release failure")
                return original_flock(descriptor, operation)

            with (
                patch.object(fcntl, "flock", side_effect=fail_release),
                self.assertRaises(ConfigLineageError) as release_caught,
            ):
                with directory.lock(".release-failure.lock"):
                    pass
            self.assertEqual(CONFIG_LINEAGE_UNSAFE, release_caught.exception.code)
            self.assertIn("anchored lock release failed", str(release_caught.exception))

    def test_lock_descriptor_close_failure_is_typed_and_takes_precedence(self) -> None:
        original_open = os.open
        original_close = os.close

        with config_lineage_module._open_absolute_directory(
            self.root,
            create=False,
        ) as directory:
            for index, body_error in enumerate((None, RuntimeError("body sentinel"))):
                with self.subTest(body_error=body_error is not None):
                    lock_name = f".close-failure-{index}.lock"
                    lock_descriptor: int | None = None
                    close_attempts = 0
                    close_error = OSError("close failure")

                    def capture_lock_open(path, flags, mode=0o777, *, dir_fd=None):
                        nonlocal lock_descriptor
                        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
                        if (
                            lock_descriptor is None
                            and path == lock_name
                            and dir_fd == directory.descriptor
                        ):
                            lock_descriptor = descriptor
                        return descriptor

                    def close_lock_then_fail(descriptor):
                        nonlocal close_attempts
                        if descriptor == lock_descriptor:
                            close_attempts += 1
                            original_close(descriptor)
                            raise close_error
                        return original_close(descriptor)

                    with (
                        patch.object(os, "open", side_effect=capture_lock_open),
                        patch.object(os, "close", side_effect=close_lock_then_fail),
                        self.assertRaises(ConfigLineageError) as caught,
                    ):
                        with directory.lock(lock_name):
                            if body_error is not None:
                                raise body_error
                    self.assertEqual(CONFIG_LINEAGE_UNSAFE, caught.exception.code)
                    self.assertIn("anchored lock release failed", str(caught.exception))
                    self.assertIs(close_error, caught.exception.__cause__)
                    self.assertEqual(1, close_attempts)

    def test_publish_is_append_only_and_idempotent(self) -> None:
        first = self._publish()
        second = self._publish()
        self.assertFalse(first.idempotent)
        self.assertTrue(second.idempotent)
        self.assertEqual(first.generation_id, second.generation_id)
        self.assertEqual(
            {"confirmed_config.canonical-lf.yaml", "config_lineage_reconfirmation.v1.json"},
            {path.name for path in first.generation_dir.iterdir()},
        )
        manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual("same_text_content_reconfirmed", manifest["proof"]["content"])
        self.assertEqual(
            "historical_raw_snapshot_not_observed",
            manifest["proof"]["historical_snapshot"],
        )
        self.assertFalse(manifest["projection"]["historical_raw_snapshot_observed"])

        loaded = load_config_lineage_reconfirmation(
            self.lineage_root,
            target_run_id="run-1",
            trusted_config_root=self.config_root,
            observed_config_path=self.config_path,
            confirmed_config_name="confirmed.yaml",
            confirmed_text_sha256=self.inspection.confirmed_text_sha256,
            expected_workflow_bindings=_bindings(),
        )
        self.assertEqual(first.generation_id, loaded.generation_id)

    def test_concurrent_publish_has_one_generation_and_is_idempotent(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self._publish(), range(2)))

        self.assertEqual(1, len({result.generation_id for result in results}))
        self.assertEqual([False, True], sorted(result.idempotent for result in results))
        generations = self.lineage_root / "run-1" / "generations"
        self.assertEqual(1, len(list(generations.iterdir())))

    def test_failed_staging_write_is_not_published_and_retry_succeeds(self) -> None:
        with patch.object(
            config_lineage_module._AnchoredDir,
            "write_exclusive",
            side_effect=OSError("injected write failure"),
        ):
            with self.assertRaisesRegex(OSError, "injected"):
                self._publish()

        generations = self.lineage_root / "run-1" / "generations"
        self.assertEqual([], list(generations.iterdir()))
        self.assertFalse(any(path.name.startswith(".staging-") for path in generations.iterdir()))
        published = self._publish()
        self.assertFalse(published.idempotent)

    def test_conflicting_generation_or_byte_change_fails_closed(self) -> None:
        first = self._publish()
        extra = first.generation_dir.parent / "lineage-deadbeefdeadbeefdeadbeef"
        extra.mkdir()
        with self.assertRaisesRegex(ConfigLineageError, "conflict"):
            self._publish()
        extra.rmdir()
        first.manifest_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(ConfigLineageError):
            self._publish()

    def test_mismatch_does_not_change_observed_or_historical_files(self) -> None:
        before = self.config_path.read_bytes()
        historical = self.root / "historical-run.json"
        historical.write_bytes(b'{"immutable":true}\n')
        historical_before = historical.read_bytes()
        with self.assertRaisesRegex(ConfigLineageError, "mismatch"):
            self._publish(confirmed_text_sha256="f" * 64)
        self.assertEqual(before, self.config_path.read_bytes())
        self.assertEqual(historical_before, historical.read_bytes())
        self.assertFalse(self.lineage_root.exists())

    def test_links_hardlinks_and_identity_aliases_are_rejected(self) -> None:
        hardlink = self.config_root / "alias.yaml"
        try:
            os.link(self.config_path, hardlink)
        except OSError:
            self.skipTest("hard links are unavailable")
        with self.assertRaises(ConfigLineageError) as caught:
            self._publish()
        self.assertEqual(CONFIG_LINEAGE_UNSAFE, caught.exception.code)
        self.assertIn("hard link or identity alias", str(caught.exception))

    def test_symlinked_observed_file_is_rejected(self) -> None:
        target = self.config_root / "target.yaml"
        target.write_bytes(self.config_path.read_bytes())
        link = self.config_root / "link.yaml"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlinks are unavailable")
        with self.assertRaises(ConfigLineageError):
            self._publish(
                observed_config_path=link,
                confirmed_config_name="link.yaml",
                expected_observed_raw_sha256=inspect_config_bytes(target.read_bytes()).observed_raw_sha256,
            )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is POSIX-only")
    def test_fifo_is_rejected_without_blocking(self) -> None:
        fifo = self.config_root / "config.fifo"
        os.mkfifo(fifo)
        with self.assertRaises(ConfigLineageError):
            self._publish(
                observed_config_path=fifo,
                confirmed_config_name="config.fifo",
            )

    def test_operator_and_reviewer_must_be_independent(self) -> None:
        with self.assertRaisesRegex(ConfigLineageError, "independent"):
            self._publish(reviewer_id="operator-1")

    def test_workflow_artifact_bindings_must_be_exact_sha256_envelopes(self) -> None:
        bindings = _bindings()
        bindings["request"] = {
            "sha256": "2" * 64,
            "untrusted_path": "request.json",
        }
        with self.assertRaisesRegex(ConfigLineageError, "exact SHA-256"):
            self._publish(workflow_bindings=bindings)

        bindings = _bindings()
        bindings["calibration"] = {"sha256": "not-a-sha"}
        with self.assertRaisesRegex(ConfigLineageError, "exact SHA-256"):
            self._publish(workflow_bindings=bindings)

    def test_load_revalidates_authoritative_workflow_bindings(self) -> None:
        self._publish()
        changed = _bindings()
        changed["historical_full_runs"][1]["notes_sha256"] = "d" * 64
        with self.assertRaisesRegex(ConfigLineageError, "workflow|mismatch"):
            load_config_lineage_reconfirmation(
                self.lineage_root,
                target_run_id="run-1",
                trusted_config_root=self.config_root,
                observed_config_path=self.config_path,
                confirmed_config_name="confirmed.yaml",
                confirmed_text_sha256=self.inspection.confirmed_text_sha256,
                expected_workflow_bindings=changed,
            )

    def test_parent_component_swap_is_rejected_by_open_handle_chain(self) -> None:
        nested = self.config_root / "nested"
        nested.mkdir()
        nested_config = nested / self.config_path.name
        nested_config.write_bytes(self.config_path.read_bytes())
        original_child = config_lineage_module._AnchoredDir.child
        swapped = False

        def swap_after_open(directory, name, *, create=False):
            nonlocal swapped
            child = original_child(directory, name, create=create)
            if name == "nested" and not swapped:
                swapped = True
                moved = nested.with_name("nested-original")
                nested.rename(moved)
                nested.mkdir()
                (nested / self.config_path.name).write_bytes(b"attacker: true\n")
            return child

        with (
            patch.object(config_lineage_module._AnchoredDir, "child", swap_after_open),
            self.assertRaisesRegex(ConfigLineageError, "identity|unavailable"),
        ):
            self._publish(observed_config_path=nested_config)

    def test_lock_name_swap_is_rejected_before_unlock(self) -> None:
        first = self._publish()
        target_root = first.generation_dir.parent.parent
        lock_path = target_root / ".config-lineage.lock"
        original_names = config_lineage_module._anchored_visible_generation_names
        swapped = False

        def swap_lock(generations):
            nonlocal swapped
            result = original_names(generations)
            if not swapped:
                swapped = True
                lock_path.rename(target_root / ".lock-original")
                lock_path.write_bytes(b"\0")
            return result

        with (
            patch.object(
                config_lineage_module,
                "_anchored_visible_generation_names",
                side_effect=swap_lock,
            ),
            self.assertRaisesRegex(ConfigLineageError, "lock identity"),
        ):
            self._publish()

    def test_generation_replay_swap_is_rejected(self) -> None:
        generation = self._publish()
        original_read = config_lineage_module._AnchoredDir.read_regular
        swapped = False

        def swap_generation(directory, name):
            nonlocal swapped
            if name == "confirmed_config.canonical-lf.yaml" and not swapped:
                swapped = True
                moved = generation.generation_dir.with_name(
                    generation.generation_dir.name + "-original"
                )
                generation.generation_dir.rename(moved)
                generation.generation_dir.mkdir()
                (generation.generation_dir / name).write_bytes(b"attacker: true\n")
                (
                    generation.generation_dir
                    / "config_lineage_reconfirmation.v1.json"
                ).write_text("{}\n", encoding="utf-8")
            return original_read(directory, name)

        with (
            patch.object(
                config_lineage_module._AnchoredDir,
                "read_regular",
                swap_generation,
            ),
            self.assertRaisesRegex(ConfigLineageError, "identity|unavailable"),
        ):
            load_config_lineage_reconfirmation(
                self.lineage_root,
                target_run_id="run-1",
                trusted_config_root=self.config_root,
                observed_config_path=self.config_path,
                confirmed_config_name="confirmed.yaml",
                confirmed_text_sha256=self.inspection.confirmed_text_sha256,
                expected_workflow_bindings=_bindings(),
            )

    def test_staging_name_swap_is_rejected_before_publish(self) -> None:
        original_write = config_lineage_module._AnchoredDir.write_exclusive
        swapped = False

        def swap_staging(directory, name, content):
            nonlocal swapped
            original_write(directory, name, content)
            if name == "config_lineage_reconfirmation.v1.json" and not swapped:
                swapped = True
                moved = directory.path.with_name(directory.path.name + "-original")
                directory.path.rename(moved)
                directory.path.mkdir()

        with (
            patch.object(
                config_lineage_module._AnchoredDir,
                "write_exclusive",
                swap_staging,
            ),
            self.assertRaisesRegex(ConfigLineageError, "identity|unavailable"),
        ):
            self._publish()


if __name__ == "__main__":
    unittest.main()
