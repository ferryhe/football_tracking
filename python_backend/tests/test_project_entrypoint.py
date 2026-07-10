from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import project


class ProjectEntrypointTests(unittest.TestCase):
    def test_repo_root_is_workspace_root(self) -> None:
        self.assertEqual(Path(__file__).resolve().parents[2], project.repo_root())

    def test_virtualenv_python_supports_windows_and_posix_layouts(self) -> None:
        root = Path("/workspace")

        self.assertEqual(root / ".venv" / "Scripts" / "python.exe", project.virtualenv_python(root, "nt"))
        self.assertEqual(root / ".venv" / "bin" / "python", project.virtualenv_python(root, "posix"))

    def test_build_project_env_prepends_backend_and_preserves_existing_pythonpath(self) -> None:
        root = Path("/workspace").resolve()
        env = project.build_project_env(root, {"PYTHONPATH": "existing-path", "KEEP": "yes"})

        self.assertEqual("yes", env["KEEP"])
        self.assertEqual("/", env["BASE_PATH"])
        self.assertEqual("5173", env["PORT"])
        self.assertEqual(
            os.pathsep.join((str(root / "python_backend"), "existing-path")),
            env["PYTHONPATH"],
        )

    def test_build_project_env_does_not_duplicate_backend(self) -> None:
        root = Path("/workspace").resolve()
        backend = str(root / "python_backend")

        env = project.build_project_env(root, {"PYTHONPATH": os.pathsep.join((backend, "other"))})

        self.assertEqual(os.pathsep.join((backend, "other")), env["PYTHONPATH"])

    def test_build_project_env_moves_an_existing_backend_to_the_front(self) -> None:
        root = Path("/workspace").resolve()
        backend = str(root / "python_backend")

        env = project.build_project_env(root, {"PYTHONPATH": os.pathsep.join(("other", backend))})

        self.assertEqual(os.pathsep.join((backend, "other")), env["PYTHONPATH"])

    def test_pnpm_argument_separator_is_removed_before_parsing(self) -> None:
        args = project.parse_args(["start", "--", "--backend-port", "18080"])

        self.assertEqual("start", args.command)
        self.assertEqual(18080, args.backend_port)

    def test_package_scripts_bootstrap_the_root_virtualenv_through_node(self) -> None:
        root = project.repo_root()
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        for name in ("start", "stop", "status", "check", "test", "train", "validate:full-video"):
            self.assertTrue(package["scripts"][name].startswith("node scripts/project.mjs "))

    def test_node_bootstrap_does_not_require_system_python_on_path(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        env = dict(os.environ)
        env["PATH"] = str(Path(node).parent)

        completed = subprocess.run(
            [node, str(project.repo_root() / "scripts" / "project.mjs"), "check"],
            cwd=project.repo_root(),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("ROOT_VIRTUALENV=OK", completed.stdout)

    def test_node_pnpm_launcher_supports_pnpm_only_and_corepack_only_paths(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        launcher = project.repo_root() / "scripts" / "run-pnpm.mjs"
        for executable_name, expected in (("pnpm", "alpha beta"), ("corepack", "pnpm alpha beta")):
            with self.subTest(executable=executable_name), tempfile.TemporaryDirectory() as temp_dir:
                suffix = ".cmd" if os.name == "nt" else ""
                executable = Path(temp_dir) / f"{executable_name}{suffix}"
                if os.name == "nt":
                    executable.write_text("@echo off\necho %*\nexit /b 0\n", encoding="utf-8")
                else:
                    executable.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n", encoding="utf-8")
                    executable.chmod(0o755)
                env = dict(os.environ)
                env["PATH"] = temp_dir

                completed = subprocess.run(
                    [node, str(launcher), "alpha", "beta"],
                    cwd=project.repo_root(),
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertIn(expected, completed.stdout)

    def test_training_flags_after_pnpm_separator_are_forwarded(self) -> None:
        args = project.parse_args(["train", "--", "--dataset", "samples.jsonl"])

        self.assertEqual(["--dataset", "samples.jsonl"], args.forward_args)

    def test_run_script_uses_selected_python_root_cwd_args_and_environment(self) -> None:
        root = Path("/workspace")
        python_exe = root / ".venv" / "bin" / "python"
        script = root / "python_backend" / "scripts" / "worker.py"
        completed = type("Completed", (), {"returncode": 7})()

        with patch("scripts.project.subprocess.run", return_value=completed) as run_mock:
            result = project.run_script(
                python_exe,
                script,
                ["--flag", "value"],
                root=root,
                env={"PYTHONPATH": "backend"},
            )

        self.assertEqual(7, result)
        run_mock.assert_called_once_with(
            [str(python_exe), str(script), "--flag", "value"],
            cwd=str(root),
            env={"PYTHONPATH": "backend"},
            check=False,
        )

    def test_windows_virtualenv_restart_waits_for_child_and_returns_exit_code(self) -> None:
        root = Path("C:/workspace")
        python_exe = root / ".venv" / "Scripts" / "python.exe"
        completed = type("Completed", (), {"returncode": 23})()

        with patch("scripts.project.subprocess.run", return_value=completed) as run_mock:
            result = project.restart_in_virtualenv(
                python_exe,
                ["test", "--python-only"],
                {"PATH": "existing"},
                root=root,
                platform_name="nt",
            )

        self.assertEqual(23, result)
        command = run_mock.call_args.args[0]
        self.assertEqual(str(python_exe), command[0])
        self.assertEqual(["test", "--python-only"], command[2:])
        self.assertEqual(str(root), run_mock.call_args.kwargs["cwd"])

    def test_run_tests_python_only_uses_official_openapi_and_full_discovery_routes(self) -> None:
        root = Path("/workspace")
        python_exe = root / ".venv" / "bin" / "python"
        commands: list[list[str]] = []

        def record(command: list[str], **_: object) -> int:
            commands.append(command)
            return 0

        result = project.run_tests(
            root,
            python_exe,
            {"PYTHONPATH": "backend"},
            python_only=True,
            pattern="test_*.py",
            runner=record,
        )

        self.assertEqual(0, result)
        self.assertEqual(
            [
                [str(python_exe), str(root / "python_backend" / "scripts" / "export_openapi.py"), "--check"],
                [
                    str(python_exe),
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "python_backend/tests",
                    "-p",
                    "test_*.py",
                ],
            ],
            commands,
        )

    def test_run_tests_default_includes_broadcast_tests_typecheck_and_build(self) -> None:
        root = Path("/workspace")
        python_exe = root / ".venv" / "bin" / "python"
        commands: list[list[str]] = []

        def record(command: list[str], **_: object) -> int:
            commands.append(command)
            return 0

        with patch("scripts.project.pnpm_command", side_effect=lambda *args: ["pnpm", *args]):
            result = project.run_tests(
                root,
                python_exe,
                {"PYTHONPATH": "backend"},
                runner=record,
            )

        self.assertEqual(0, result)
        self.assertIn(["pnpm", "--filter", "@workspace/web", "run", "test:broadcast-workflow"], commands)
        self.assertIn(["pnpm", "--filter", "@workspace/web", "run", "test:broadcast-components"], commands)
        self.assertIn(["pnpm", "run", "typecheck"], commands)
        self.assertIn(["pnpm", "-r", "--if-present", "run", "build"], commands)

    def test_run_tests_stops_at_first_failure(self) -> None:
        root = Path("/workspace")
        python_exe = root / ".venv" / "bin" / "python"
        calls = 0

        def fail_first(_: list[str], **__: object) -> int:
            nonlocal calls
            calls += 1
            return 9

        result = project.run_tests(root, python_exe, {}, runner=fail_first)

        self.assertEqual(9, result)
        self.assertEqual(1, calls)

    def test_check_project_fails_when_required_paths_are_missing(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("scripts.project.resolve_pnpm_command", return_value=None),
        ):
            self.assertEqual(1, project.check_project(Path(temp_dir)))


if __name__ == "__main__":
    unittest.main()
