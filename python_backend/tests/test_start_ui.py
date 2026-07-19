from __future__ import annotations

import json
import os
import signal
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.start_ui import (
    STATE_OWNERSHIP,
    STATE_SCHEMA_VERSION,
    build_backend_env,
    build_frontend_env,
    cleanup_managed_processes,
    find_available_port,
    identities_match,
    listening_pid_for_port,
    load_state,
    process_identity,
    repo_root,
    save_state,
    start_ui,
    status_ui,
    terminate_process_tree,
    terminate_spawned_process,
    wait_for_http_ok,
    wait_for_listener_identity,
)


def managed_state(**services: object) -> dict[str, object]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "ownership": STATE_OWNERSHIP,
        **services,
    }


class StartUiScriptTests(unittest.TestCase):
    def test_find_available_port_returns_preferred_port_when_free(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            preferred_port = probe.getsockname()[1]

        resolved_port = find_available_port(preferred_port, search_span=0)

        self.assertEqual(preferred_port, resolved_port)

    def test_find_available_port_skips_port_that_is_already_bound(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied_port = occupied.getsockname()[1]

            resolved_port = find_available_port(occupied_port, search_span=3)

        self.assertNotEqual(occupied_port, resolved_port)
        self.assertGreaterEqual(resolved_port, occupied_port + 1)

    def test_http_wait_allows_one_slow_health_request_to_use_the_remaining_deadline(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.status = 200

        with patch("scripts.start_ui.urllib.request.urlopen", return_value=response) as urlopen:
            self.assertTrue(wait_for_http_ok("http://127.0.0.1:8000/api/v1/health", timeout_seconds=12.0))

        self.assertGreater(urlopen.call_args.kwargs["timeout"], 11.0)

    def test_state_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "ui_state.json"
            state = {"backend_pid": 1234, "frontend_port": 5173}

            save_state(state_path, state)

            self.assertEqual(state, load_state(state_path))

    def test_save_state_atomically_replaces_and_fsyncs_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "ui_state.json"
            with (
                patch("scripts.start_ui.os.replace", wraps=os.replace) as replace,
                patch("scripts.start_ui.os.fsync", wraps=os.fsync) as fsync,
            ):
                save_state(state_path, {"schema_version": STATE_SCHEMA_VERSION})

            self.assertGreaterEqual(fsync.call_count, 1)
            replace.assert_called_once()
            self.assertEqual({"schema_version": STATE_SCHEMA_VERSION}, load_state(state_path))
            self.assertEqual([state_path], list(Path(temp_dir).iterdir()))

    def test_build_frontend_env_injects_backend_and_frontend_ports(self) -> None:
        env = build_frontend_env({"PATH": "test-path"}, "127.0.0.1", 8001, 5174)

        self.assertEqual("test-path", env["PATH"])
        self.assertEqual("http://127.0.0.1:8001", env["VITE_API_PROXY_TARGET"])
        self.assertEqual("/", env["BASE_PATH"])
        self.assertEqual("5174", env["PORT"])
        self.assertEqual("5174", env["FT_FRONTEND_PORT"])
        self.assertEqual("8001", env["FT_BACKEND_PORT"])

    def test_build_backend_env_moves_backend_to_the_front(self) -> None:
        root = Path("/workspace").resolve()
        backend = str(root / "python_backend")

        env = build_backend_env({"PYTHONPATH": os.pathsep.join(("other", backend))}, root)

        self.assertEqual(os.pathsep.join((backend, "other")), env["PYTHONPATH"])

    def test_start_requires_frontend_route_and_api_proxy_health(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            python_exe = root / ".venv" / "Scripts" / "python.exe"
            python_exe.parent.mkdir(parents=True)
            python_exe.touch()
            web = root / "artifacts" / "web"
            web.mkdir(parents=True)
            (web / "package.json").write_text("{}", encoding="utf-8")
            (root / "node_modules").mkdir()
            (web / "node_modules" / "vite").mkdir(parents=True)
            backend_process = MagicMock(pid=101)
            frontend_process = MagicMock(pid=202)

            with (
                patch("scripts.start_ui.resolve_pnpm_command", return_value=["pnpm"]),
                patch("scripts.start_ui.cleanup_managed_processes", return_value=False),
                patch("scripts.start_ui.find_available_port", side_effect=lambda port, **_: port),
                patch("scripts.start_ui.spawn_background", side_effect=[backend_process, frontend_process]),
                patch(
                    "scripts.start_ui.wait_for_process_identity",
                    side_effect=[
                        {"pid": "101", "command": "backend-root", "started": "1"},
                        {"pid": "202", "command": "frontend-root", "started": "2"},
                    ],
                ),
                patch("scripts.start_ui.wait_for_http_ok", return_value=True) as wait,
                patch(
                    "scripts.start_ui.wait_for_listener_identity",
                    side_effect=[
                        (111, {"pid": "111", "command": "backend", "started": "11"}),
                        (222, {"pid": "222", "command": "web", "started": "22"}),
                    ],
                ),
                patch("scripts.start_ui.is_process_descendant", return_value=True),
                patch(
                    "scripts.start_ui.process_identity",
                    side_effect=[
                        {"pid": "101", "command": "backend-root-exec", "started": "1"},
                        {"pid": "202", "command": "frontend-root-exec", "started": "2"},
                    ],
                ),
            ):
                result = start_ui(root, python_exe, "127.0.0.1", 18000, 15173, False)

            self.assertEqual(0, result)
            self.assertEqual(
                [
                    "http://127.0.0.1:18000/api/v1/health",
                    "http://127.0.0.1:15173/broadcast",
                    "http://127.0.0.1:15173/api/healthz",
                ],
                [call.args[0] for call in wait.call_args_list],
            )
            self.assertEqual(
                [
                    {"timeout_seconds": 180.0},
                    {"timeout_seconds": 45.0},
                    {"timeout_seconds": 45.0},
                ],
                [call.kwargs for call in wait.call_args_list],
            )
            state = load_state(root / ".run" / "ui_processes.json")
            self.assertIsNotNone(state)
            self.assertEqual(101, state["backend_pid"] if state else None)
            self.assertEqual(111, state["backend_listener_pid"] if state else None)
            self.assertEqual("11", state["backend_listener_identity"]["started"] if state else None)
            self.assertEqual(202, state["frontend_pid"] if state else None)
            self.assertEqual(222, state["frontend_listener_pid"] if state else None)
            self.assertEqual("22", state["frontend_listener_identity"]["started"] if state else None)
            self.assertEqual(STATE_SCHEMA_VERSION, state["schema_version"] if state else None)
            self.assertEqual(STATE_OWNERSHIP, state["ownership"] if state else None)

    def test_start_rejects_listener_owned_by_external_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            python_exe = root / ".venv" / "Scripts" / "python.exe"
            python_exe.parent.mkdir(parents=True)
            python_exe.touch()
            web = root / "artifacts" / "web"
            web.mkdir(parents=True)
            (web / "package.json").write_text("{}", encoding="utf-8")
            (root / "node_modules").mkdir()
            (web / "node_modules" / "vite").mkdir(parents=True)
            backend_process = MagicMock(pid=101)

            with (
                patch("scripts.start_ui.resolve_pnpm_command", return_value=["pnpm"]),
                patch("scripts.start_ui.cleanup_managed_processes", return_value=False),
                patch("scripts.start_ui.find_available_port", return_value=18000),
                patch("scripts.start_ui.spawn_background", return_value=backend_process),
                patch(
                    "scripts.start_ui.wait_for_process_identity",
                    return_value={"pid": "101", "command": "backend-root", "started": "1"},
                ),
                patch("scripts.start_ui.wait_for_http_ok", return_value=True),
                patch(
                    "scripts.start_ui.wait_for_listener_identity",
                    return_value=(999, {"pid": "999", "command": "external", "started": "9"}),
                ),
                patch("scripts.start_ui.is_process_descendant", return_value=False),
                patch("scripts.start_ui.terminate_process_tree", return_value=True) as terminate,
            ):
                with self.assertRaisesRegex(RuntimeError, "not owned"):
                    start_ui(root, python_exe, "127.0.0.1", 18000, 15173, False)

            self.assertFalse((root / ".run" / "ui_processes.json").exists())
            terminate.assert_called_once_with(
                101,
                {"pid": "101", "command": "backend-root", "started": "1"},
                timeout_seconds=5.0,
            )

    def test_startup_identity_failure_terminates_spawn_handle_without_pid_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            process = MagicMock(pid=101)
            process.poll.return_value = None
            process.wait.return_value = 0

            with (
                patch("scripts.start_ui.ensure_requirements"),
                patch("scripts.start_ui.cleanup_managed_processes", return_value=False),
                patch("scripts.start_ui.find_available_port", return_value=8000),
                patch("scripts.start_ui.spawn_background", return_value=process),
                patch("scripts.start_ui.wait_for_process_identity", return_value=None),
                patch("scripts.start_ui.terminate_process_tree") as terminate,
            ):
                with self.assertRaisesRegex(RuntimeError, "root process identity"):
                    start_ui(root, root / ".venv" / "python", "127.0.0.1", 8000, 5173, False)

            process.terminate.assert_called_once_with()
            process.wait.assert_called_once()
            terminate.assert_not_called()

    def test_startup_root_exit_with_uncaptured_listener_retains_diagnostic_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            process = MagicMock(pid=101)
            process.poll.return_value = 0

            with (
                patch("scripts.start_ui.ensure_requirements"),
                patch("scripts.start_ui.cleanup_managed_processes", return_value=False),
                patch("scripts.start_ui.find_available_port", return_value=8000),
                patch("scripts.start_ui.spawn_background", return_value=process),
                patch("scripts.start_ui.wait_for_process_identity", return_value=None),
                patch("scripts.start_ui.listening_pid_for_port", return_value=999),
            ):
                with self.assertRaisesRegex(RuntimeError, "unproven backend listener remains"):
                    start_ui(root, root / ".venv" / "python", "127.0.0.1", 8000, 5173, False)

            retained = load_state(root / ".run" / "ui_processes.json")
            self.assertEqual(101, retained["backend_pid"] if retained else None)
            self.assertIn("unproven backend listener", retained["cleanup_diagnostic"] if retained else "")

    def test_later_startup_failure_cleans_captured_listener_after_root_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend_process = MagicMock(pid=101)
            frontend_process = MagicMock(pid=202)
            backend_root = {"pid": "101", "command": "backend-root", "started": "1"}
            frontend_root = {"pid": "202", "command": "frontend-root", "started": "2"}
            listener_identity = {"pid": "111", "command": "backend-worker", "started": "11"}

            with (
                patch("scripts.start_ui.ensure_requirements"),
                patch("scripts.start_ui.cleanup_managed_processes", return_value=False),
                patch("scripts.start_ui.resolve_pnpm_command", return_value=["pnpm"]),
                patch("scripts.start_ui.find_available_port", side_effect=lambda port, **_: port),
                patch("scripts.start_ui.spawn_background", side_effect=[backend_process, frontend_process]),
                patch(
                    "scripts.start_ui.wait_for_process_identity",
                    side_effect=[backend_root, frontend_root],
                ),
                patch("scripts.start_ui.wait_for_http_ok", side_effect=[True, False]),
                patch(
                    "scripts.start_ui.wait_for_listener_identity",
                    return_value=(111, listener_identity),
                ),
                patch("scripts.start_ui.is_process_descendant", return_value=True),
                patch(
                    "scripts.start_ui.process_identity",
                    side_effect=[backend_root, listener_identity, None],
                ),
                patch("scripts.start_ui.listening_pid_for_port", side_effect=[None, 111, None]),
                patch("scripts.start_ui.terminate_process_tree", return_value=True) as terminate,
            ):
                with self.assertRaisesRegex(RuntimeError, "Frontend failed route check"):
                    start_ui(root, root / ".venv" / "python", "127.0.0.1", 8000, 5173, False)

            self.assertEqual(
                [202, 101, 111],
                [call.args[0] for call in terminate.call_args_list],
            )
            self.assertFalse((root / ".run" / "ui_processes.json").exists())

    def test_repo_root_is_workspace_root(self) -> None:
        self.assertEqual(Path(__file__).resolve().parents[2], repo_root())

    def test_cleanup_managed_processes_kills_known_processes_and_removes_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "ui_state.json"
            save_state(
                state_path,
                managed_state(
                    backend_pid=101,
                    backend_port=8000,
                    backend_identity={"pid": "101", "command": "backend", "started": "1"},
                    backend_listener_pid=111,
                    backend_listener_identity={"pid": "111", "command": "backend-worker", "started": "11"},
                    frontend_pid=202,
                    frontend_port=5173,
                    frontend_identity={"pid": "202", "command": "frontend", "started": "2"},
                    frontend_listener_pid=222,
                    frontend_listener_identity={"pid": "222", "command": "frontend-worker", "started": "22"},
                ),
            )

            with (
                patch("scripts.start_ui.is_process_running", return_value=True),
                patch(
                    "scripts.start_ui.process_identity",
                    side_effect=[
                        {"pid": "202", "command": "frontend-exec", "started": "2"},
                        None,
                        {"pid": "101", "command": "backend-exec", "started": "1"},
                        None,
                    ],
                ),
                patch("scripts.start_ui.listening_pid_for_port", return_value=None),
                patch("scripts.start_ui.terminate_process_tree", return_value=True) as terminate_process_tree,
            ):
                did_cleanup = cleanup_managed_processes(state_path)

            self.assertTrue(did_cleanup)
            self.assertFalse(state_path.exists())
            self.assertEqual(2, terminate_process_tree.call_count)

    def test_cleanup_retains_diagnostic_without_killing_unproven_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "ui_state.json"
            save_state(
                state_path,
                managed_state(
                    backend_pid=101,
                    backend_port=8000,
                    backend_identity={"pid": "101", "command": "backend", "started": "1"},
                    backend_listener_pid=111,
                    backend_listener_identity={"pid": "111", "command": "worker", "started": "11"},
                ),
            )

            with (
                patch(
                    "scripts.start_ui.process_identity",
                    side_effect=[
                        {"pid": "101", "command": "reused", "started": "9"},
                        {"pid": "404", "command": "external", "started": "44"},
                    ],
                ),
                patch("scripts.start_ui.listening_pid_for_port", return_value=404),
                patch("scripts.start_ui.terminate_process_tree") as terminate_process_tree,
            ):
                with self.assertRaisesRegex(RuntimeError, "ownership could not be proven"):
                    cleanup_managed_processes(state_path)

            retained = load_state(state_path)
            self.assertEqual(101, retained["backend_pid"] if retained else None)
            self.assertIn("backend", retained["cleanup_diagnostic"] if retained else "")
            terminate_process_tree.assert_not_called()

    def test_cleanup_stops_root_even_when_reload_listener_pid_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "ui_state.json"
            identity = {"pid": "101", "command": "uvicorn --reload", "started": "1"}
            save_state(
                state_path,
                managed_state(
                    backend_pid=101,
                    backend_port=8000,
                    backend_identity=identity,
                    backend_listener_pid=111,
                    backend_listener_identity={"pid": "111", "command": "worker", "started": "11"},
                ),
            )

            with (
                patch(
                    "scripts.start_ui.process_identity",
                    side_effect=[
                        {"pid": "101", "command": "uvicorn re-exec", "started": "1"},
                        None,
                    ],
                ),
                patch("scripts.start_ui.listening_pid_for_port", return_value=None),
                patch("scripts.start_ui.terminate_process_tree", return_value=True) as terminate,
            ):
                self.assertTrue(cleanup_managed_processes(state_path))

            terminate.assert_called_once_with(101, identity)
            self.assertFalse(state_path.exists())

    def test_cleanup_failure_retains_owned_state_and_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "ui_state.json"
            identity = {"pid": "101", "command": "backend", "started": "1"}
            state = managed_state(backend_pid=101, backend_port=8000, backend_identity=identity)
            save_state(state_path, state)

            with (
                patch("scripts.start_ui.process_identity", return_value=identity),
                patch("scripts.start_ui.terminate_process_tree", return_value=False) as terminate,
            ):
                with self.assertRaisesRegex(RuntimeError, "could not be terminated"):
                    cleanup_managed_processes(state_path)

            retained = load_state(state_path)
            self.assertEqual(101, retained["backend_pid"] if retained else None)
            self.assertIn("could not be terminated", retained["cleanup_diagnostic"] if retained else "")
            terminate.assert_called_once_with(101, identity)

    def test_cleanup_stops_exact_listener_orphan_when_root_has_exited(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "ui_state.json"
            root_identity = {"pid": "101", "command": "root", "started": "1"}
            listener_identity = {"pid": "111", "command": "worker", "started": "11"}
            save_state(
                state_path,
                managed_state(
                    backend_pid=101,
                    backend_port=8000,
                    backend_identity=root_identity,
                    backend_listener_pid=111,
                    backend_listener_identity=listener_identity,
                ),
            )

            with (
                patch(
                    "scripts.start_ui.process_identity",
                    side_effect=[None, {"pid": "111", "command": "worker-exec", "started": "11"}],
                ),
                patch("scripts.start_ui.listening_pid_for_port", return_value=111),
                patch("scripts.start_ui.terminate_process_tree", return_value=True) as terminate,
            ):
                self.assertTrue(cleanup_managed_processes(state_path))

            terminate.assert_called_once_with(111, listener_identity)
            self.assertFalse(state_path.exists())

    def test_legacy_state_only_stops_exact_identity_and_port_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "ui_state.json"
            legacy_identity = {"command": "legacy-listener", "started": "11"}
            save_state(
                state_path,
                {
                    "backend_pid": 111,
                    "backend_port": 8000,
                    "backend_identity": legacy_identity,
                    "reload": False,
                },
            )

            with (
                patch("scripts.start_ui.listening_pid_for_port", return_value=111),
                patch(
                    "scripts.start_ui.process_identity",
                    return_value={"pid": "111", "command": "legacy-exec", "started": "11"},
                ),
                patch("scripts.start_ui.terminate_process_tree", return_value=True) as terminate,
                patch("scripts.start_ui._legacy_port_stays_clear", return_value=True),
            ):
                self.assertTrue(cleanup_managed_processes(state_path))

            terminate.assert_called_once_with(111, legacy_identity)
            self.assertFalse(state_path.exists())

    def test_legacy_reload_respawn_retains_state_after_worker_kill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "ui_state.json"
            legacy_identity = {"command": "legacy-worker", "started": "11"}
            save_state(
                state_path,
                {
                    "backend_pid": 111,
                    "backend_port": 8000,
                    "backend_identity": legacy_identity,
                    "reload": True,
                },
            )

            with (
                patch("scripts.start_ui.listening_pid_for_port", return_value=111),
                patch(
                    "scripts.start_ui.process_identity",
                    return_value={"pid": "111", "command": "legacy-worker", "started": "11"},
                ),
                patch("scripts.start_ui.terminate_process_tree", return_value=True),
                patch("scripts.start_ui._legacy_port_stays_clear", return_value=False),
            ):
                with self.assertRaisesRegex(RuntimeError, "reload parent ownership is unknown"):
                    cleanup_managed_processes(state_path)

            retained = load_state(state_path)
            self.assertEqual(111, retained["backend_pid"] if retained else None)
            self.assertIn("reload parent ownership is unknown", retained["cleanup_diagnostic"] if retained else "")

    def test_legacy_state_mismatch_is_retained_without_killing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "ui_state.json"
            save_state(
                state_path,
                {
                    "backend_pid": 111,
                    "backend_port": 8000,
                    "backend_identity": {"command": "legacy-listener", "started": "11"},
                },
            )

            with (
                patch("scripts.start_ui.listening_pid_for_port", return_value=222),
                patch(
                    "scripts.start_ui.process_identity",
                    return_value={"pid": "222", "command": "external", "started": "22"},
                ),
                patch("scripts.start_ui.terminate_process_tree") as terminate,
            ):
                with self.assertRaisesRegex(RuntimeError, "legacy state ownership could not be proven"):
                    cleanup_managed_processes(state_path)

            self.assertTrue(state_path.exists())
            self.assertIn("legacy", load_state(state_path)["cleanup_diagnostic"])
            terminate.assert_not_called()

    def test_start_does_not_overwrite_state_when_previous_cleanup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / ".run" / "ui_processes.json"
            previous = {"backend_pid": 101, "backend_identity": {"command": "old", "started": "1"}}
            save_state(state_path, previous)
            python_exe = root / ".venv" / "Scripts" / "python.exe"

            with (
                patch("scripts.start_ui.ensure_requirements"),
                patch("scripts.start_ui.cleanup_managed_processes", side_effect=RuntimeError("cleanup failed")),
                patch("scripts.start_ui.spawn_background") as spawn,
            ):
                with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                    start_ui(root, python_exe, "127.0.0.1", 8000, 5173, False)

            self.assertEqual(previous, load_state(state_path))
            spawn.assert_not_called()

    def test_status_accepts_changed_listener_when_it_remains_in_root_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / ".run" / "ui_processes.json"
            backend_identity = {"pid": "101", "command": "backend-root", "started": "1"}
            frontend_identity = {"pid": "202", "command": "frontend-root", "started": "2"}
            save_state(
                state_path,
                managed_state(
                    backend_pid=101,
                    backend_port=8000,
                    backend_identity=backend_identity,
                    frontend_pid=202,
                    frontend_port=5173,
                    frontend_identity=frontend_identity,
                ),
            )

            with (
                patch(
                    "scripts.start_ui.process_identity",
                    side_effect=[
                        {"pid": "101", "command": "backend-reexec", "started": "1"},
                        {"pid": "202", "command": "frontend-reexec", "started": "2"},
                    ],
                ),
                patch("scripts.start_ui.listening_pid_for_port", side_effect=[303, 404]),
                patch("scripts.start_ui.is_process_descendant", return_value=True),
            ):
                self.assertEqual(0, status_ui(root))

    def test_terminate_process_tree_waits_for_confirmed_exit(self) -> None:
        expected = {"pid": "101", "command": "backend", "started": "1"}
        with (
            patch("scripts.start_ui.os.name", "nt"),
            patch("scripts.start_ui.subprocess.run") as run,
            patch(
                "scripts.start_ui.process_identity",
                side_effect=[
                    expected,
                    {"pid": "101", "command": "backend-exec", "started": "1"},
                    {"pid": "101", "command": "reused", "started": "9"},
                ],
            ),
            patch("scripts.start_ui.time.sleep"),
        ):
            self.assertTrue(terminate_process_tree(101, expected, timeout_seconds=1.0))

        run.assert_called_once()

    def test_terminate_process_tree_does_not_signal_after_identity_changes(self) -> None:
        expected = {"pid": "101", "command": "backend", "started": "1"}
        reused = {"pid": "101", "command": "other", "started": "9"}
        with (
            patch("scripts.start_ui.process_identity", return_value=reused),
            patch("scripts.start_ui.subprocess.run") as run,
            patch("scripts.start_ui.os.killpg", create=True) as killpg,
        ):
            self.assertTrue(terminate_process_tree(101, expected))

        run.assert_not_called()
        killpg.assert_not_called()

    def test_posix_termination_escalates_and_rechecks_birth_identity(self) -> None:
        expected = {"pid": "101", "command": "backend", "started": "1"}
        with (
            patch("scripts.start_ui.os.name", "posix"),
            patch("scripts.start_ui.os.getpgid", return_value=101, create=True),
            patch("scripts.start_ui.os.killpg", create=True) as killpg,
            patch("scripts.start_ui.signal.SIGKILL", 9, create=True),
            patch(
                "scripts.start_ui.process_identity",
                side_effect=[expected, expected, expected, expected, None],
            ),
        ):
            self.assertTrue(terminate_process_tree(101, expected, timeout_seconds=0.0))

        self.assertEqual(
            [signal.SIGTERM, 9],
            [call.args[1] for call in killpg.call_args_list],
        )

    def test_spawn_cleanup_without_identity_uses_popen_handle(self) -> None:
        process = MagicMock(pid=101)
        process.poll.return_value = None
        process.wait.return_value = 0

        with patch("scripts.start_ui.terminate_process_tree") as terminate:
            self.assertTrue(terminate_spawned_process(process, None))

        process.terminate.assert_called_once_with()
        process.wait.assert_called_once()
        terminate.assert_not_called()

    def test_identity_matching_ignores_command_change_but_requires_birth_token(self) -> None:
        expected = {
            "pid": "101",
            "command": "python launcher.py",
            "started": "123",
            "identity_kind": "proc-start-ticks",
        }
        after_exec = {
            "pid": "101",
            "command": "uvicorn worker",
            "started": "123",
            "identity_kind": "proc-start-ticks",
        }
        reused = {
            "pid": "101",
            "command": "uvicorn worker",
            "started": "999",
            "identity_kind": "proc-start-ticks",
        }

        self.assertTrue(identities_match(101, expected, after_exec))
        self.assertFalse(identities_match(101, expected, reused))

    def test_low_resolution_ps_identity_keeps_command_as_safety_tiebreaker(self) -> None:
        expected = {
            "pid": "101",
            "command": "python launcher.py",
            "started": "Mon Jul 10 12:34:56 2026",
            "identity_kind": "ps-lstart-command",
        }
        changed = {**expected, "command": "unrelated reused process"}

        self.assertFalse(identities_match(101, expected, changed))

    def test_owned_listener_wait_rejects_external_listener(self) -> None:
        identity = {"pid": "101", "command": "backend-root", "started": "1"}
        with (
            patch("scripts.start_ui.time.monotonic", side_effect=[0.0, 0.0, 2.0]),
            patch("scripts.start_ui.time.sleep"),
            patch(
                "scripts.start_ui.process_identity",
                side_effect=[
                    {"pid": "101", "command": "backend-exec", "started": "1"},
                    {"pid": "999", "command": "external", "started": "9"},
                ],
            ),
            patch("scripts.start_ui.listening_pid_for_port", return_value=999),
            patch("scripts.start_ui.is_process_descendant", return_value=False),
        ):
            listener = wait_for_listener_identity(8000, 101, identity, timeout_seconds=1.0)

        self.assertIsNone(listener)

    def test_listening_pid_for_port_parses_windows_netstat_output(self) -> None:
        output = "\n".join(
            [
                "Active Connections",
                "",
                "  Proto  Local Address          Foreign Address        State           PID",
                "  TCP    127.0.0.1:5173         0.0.0.0:0              LISTENING       2222",
            ]
        )

        with patch("scripts.start_ui.os.name", "nt"), patch("scripts.start_ui.subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = output
            pid = listening_pid_for_port(5173)

        self.assertEqual(2222, pid)

    def test_process_identity_reads_linux_proc_files(self) -> None:
        stat_text = "101 (python worker) S " + " ".join(str(value) for value in range(1, 25))

        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_bytes", return_value=b"python\x00-m\x00uvicorn\x00"),
            patch.object(Path, "read_text", return_value=stat_text),
        ):
            identity = process_identity(101, "posix")

        self.assertEqual("python -m uvicorn", identity["command"] if identity else None)
        self.assertEqual("101", identity["pid"] if identity else None)
        self.assertIsNotNone(identity)

    def test_vite_config_rewrites_frontend_api_paths_to_fastapi(self) -> None:
        config = (repo_root() / "artifacts" / "web" / "vite.config.ts").read_text(encoding="utf-8")

        self.assertIn('path === "/api/healthz"', config)
        self.assertIn('return "/api/v1/health"', config)
        self.assertIn('path.replace(/^\\/api/, "/api/v1")', config)
        self.assertIn('process.env.FT_DEV_HOST ?? "0.0.0.0"', config)
        package = json.loads((repo_root() / "artifacts" / "web" / "package.json").read_text(encoding="utf-8"))
        self.assertNotIn("--host", package["scripts"]["dev"])


if __name__ == "__main__":
    unittest.main()
