from __future__ import annotations

import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.start_ui import (
    build_frontend_env,
    cleanup_managed_processes,
    find_available_port,
    listening_pid_for_port,
    load_state,
    save_state,
)


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

    def test_state_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "ui_state.json"
            state = {"backend_pid": 1234, "frontend_port": 5173}

            save_state(state_path, state)

            self.assertEqual(state, load_state(state_path))

    def test_build_frontend_env_injects_backend_and_frontend_ports(self) -> None:
        env = build_frontend_env({"PATH": "test-path"}, "127.0.0.1", 8001, 5174)

        self.assertEqual("test-path", env["PATH"])
        self.assertEqual("http://127.0.0.1:8001", env["VITE_API_PROXY_TARGET"])
        self.assertEqual("5174", env["FT_FRONTEND_PORT"])
        self.assertEqual("8001", env["FT_BACKEND_PORT"])

    def test_cleanup_managed_processes_kills_known_processes_and_removes_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "ui_state.json"
            save_state(
                state_path,
                {"backend_pid": 101, "backend_port": 8000, "frontend_pid": 202, "frontend_port": 5173},
            )

            with patch("scripts.start_ui.is_process_running", return_value=True), patch(
                "scripts.start_ui.is_expected_managed_process",
                return_value=True,
            ), patch(
                "scripts.start_ui.terminate_process_tree"
            ) as terminate_process_tree:
                did_cleanup = cleanup_managed_processes(state_path)

            self.assertTrue(did_cleanup)
            self.assertFalse(state_path.exists())
            self.assertEqual(2, terminate_process_tree.call_count)

    def test_cleanup_managed_processes_skips_pid_when_port_does_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "ui_state.json"
            save_state(
                state_path,
                {"backend_pid": 101, "backend_port": 8000, "frontend_pid": 202, "frontend_port": 5173},
            )

            with patch("scripts.start_ui.is_process_running", return_value=True), patch(
                "scripts.start_ui.is_expected_managed_process",
                return_value=False,
            ), patch(
                "scripts.start_ui.terminate_process_tree"
            ) as terminate_process_tree:
                did_cleanup = cleanup_managed_processes(state_path)

            self.assertFalse(did_cleanup)
            self.assertFalse(state_path.exists())
            terminate_process_tree.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
