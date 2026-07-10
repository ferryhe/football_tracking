from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def virtualenv_python(root: Path, platform_name: str | None = None) -> Path:
    platform_name = platform_name or os.name
    if platform_name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def build_project_env(root: Path, base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    backend = str((root / "python_backend").resolve())
    existing = [item for item in env.get("PYTHONPATH", "").split(os.pathsep) if item]
    normalized_backend = os.path.normcase(os.path.abspath(backend))
    existing = [item for item in existing if os.path.normcase(os.path.abspath(item)) != normalized_backend]
    existing.insert(0, backend)
    env["PYTHONPATH"] = os.pathsep.join(existing)
    env.setdefault("BASE_PATH", "/")
    env.setdefault("PORT", "5173")
    return env


def resolve_pnpm_command() -> list[str] | None:
    pnpm = shutil.which("pnpm")
    if pnpm:
        return [pnpm]
    corepack = shutil.which("corepack")
    if corepack:
        return [corepack, "pnpm"]
    return None


def pnpm_command(*args: str) -> list[str]:
    command = resolve_pnpm_command()
    if command is None:
        raise RuntimeError("pnpm was not found. Install pnpm or enable Corepack.")
    return [*command, *args]


def run_command(command: list[str], *, cwd: str, env: dict[str, str]) -> int:
    try:
        completed = subprocess.run(command, cwd=cwd, env=env, check=False)
    except KeyboardInterrupt:
        return 130
    return completed.returncode


def run_script(
    python_exe: Path,
    script: Path,
    args: Sequence[str],
    *,
    root: Path,
    env: dict[str, str],
) -> int:
    try:
        completed = subprocess.run(
            [str(python_exe), str(script), *args],
            cwd=str(root),
            env=env,
            check=False,
        )
    except KeyboardInterrupt:
        return 130
    return completed.returncode


def run_tests(
    root: Path,
    python_exe: Path,
    env: dict[str, str],
    *,
    python_only: bool = False,
    node_only: bool = False,
    pattern: str = "test_*.py",
    runner: Callable[..., int] = run_command,
) -> int:
    commands: list[list[str]] = []
    if not node_only:
        commands.extend(
            [
                [
                    str(python_exe),
                    str(root / "python_backend" / "scripts" / "export_openapi.py"),
                    "--check",
                ],
                [
                    str(python_exe),
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "python_backend/tests",
                    "-p",
                    pattern,
                ],
            ]
        )
    if not python_only:
        commands.extend(
            [
                pnpm_command("--filter", "@workspace/web", "run", "test:broadcast-workflow"),
                pnpm_command("--filter", "@workspace/web", "run", "test:broadcast-components"),
                pnpm_command("run", "typecheck"),
                pnpm_command("-r", "--if-present", "run", "build"),
            ]
        )

    for command in commands:
        print(f"[TEST] {subprocess.list2cmdline(command)}", flush=True)
        returncode = runner(command, cwd=str(root), env=env)
        if returncode != 0:
            return returncode
    return 0


def check_project(root: Path) -> int:
    python_exe = virtualenv_python(root)
    python_dependencies = False
    if python_exe.is_file():
        completed = subprocess.run(
            [
                str(python_exe),
                "-c",
                "import cv2, fastapi, football_tracking, imageio_ffmpeg, numpy, scipy, uvicorn",
            ],
            cwd=str(root),
            env=build_project_env(root),
            capture_output=True,
            text=True,
            check=False,
        )
        python_dependencies = completed.returncode == 0
    checks = {
        "root_virtualenv": python_exe.is_file(),
        "python_dependencies": python_dependencies,
        "workspace_package": (root / "package.json").is_file(),
        "web_package": (root / "artifacts" / "web" / "package.json").is_file(),
        "web_dependencies": (root / "artifacts" / "web" / "node_modules" / "vite").is_dir(),
        "input_directory": (root / "python_backend" / "data").is_dir(),
        "config_directory": (root / "python_backend" / "config").is_dir(),
        "tracking_config": any((root / "python_backend" / "config").glob("*.yaml")),
        "default_detector_weight": (root / "python_backend" / "weights" / "football_ball_yolo.pt").is_file(),
        "launcher": (root / "python_backend" / "scripts" / "start_ui.py").is_file(),
        "trainer": (root / "python_backend" / "scripts" / "train_candidate_classifier.py").is_file(),
        "full_video_validator": (root / "python_backend" / "scripts" / "validate_broadcast_run.py").is_file(),
        "pnpm": resolve_pnpm_command() is not None,
    }
    for name, ready in checks.items():
        print(f"{name.upper()}={'OK' if ready else 'MISSING'}")
    return 0 if all(checks.values()) else 1


def _same_executable(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def restart_in_virtualenv(
    python_exe: Path,
    argv: Sequence[str],
    env: dict[str, str],
    *,
    root: Path,
    platform_name: str | None = None,
) -> int:
    env = dict(env)
    env["VIRTUAL_ENV"] = str(python_exe.parent.parent)
    env["PATH"] = os.pathsep.join((str(python_exe.parent), env.get("PATH", "")))
    command = [str(python_exe), str(Path(__file__).resolve()), *argv]
    if (platform_name or os.name) == "nt":
        try:
            completed = subprocess.run(command, cwd=str(root), env=env, check=False)
        except KeyboardInterrupt:
            return 130
        return completed.returncode
    os.execve(
        str(python_exe),
        command,
        env,
    )
    raise AssertionError("os.execve returned unexpectedly")


def normalize_argv(argv: Sequence[str]) -> list[str]:
    normalized = list(argv)
    if len(normalized) > 1 and normalized[1] == "--" and normalized[0] not in {"train", "validate-full-video"}:
        del normalized[1]
    return normalized


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Official football_tracking project entrypoint.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Start the managed FastAPI and web development services.")
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--backend-port", type=int, default=8000)
    start.add_argument("--frontend-port", type=int, default=5173)
    start.add_argument("--reload", action="store_true", help="Enable uvicorn reload mode.")
    subparsers.add_parser("stop", help="Stop only the managed local services.")
    subparsers.add_parser("status", help="Check managed local service state.")
    subparsers.add_parser("check", help="Check required local project tools and files.")

    test = subparsers.add_parser("test", help="Run the official project verification suite.")
    scope = test.add_mutually_exclusive_group()
    scope.add_argument("--python-only", action="store_true", help=argparse.SUPPRESS)
    scope.add_argument("--node-only", action="store_true", help=argparse.SUPPRESS)
    test.add_argument("--pattern", default="test_*.py", help="unittest discovery pattern (focused local use).")

    train = subparsers.add_parser("train", help="Forward to candidate classifier training.")
    train.add_argument("forward_args", nargs=argparse.REMAINDER)
    validate = subparsers.add_parser("validate-full-video", help="Validate a completed full-video broadcast run.")
    validate.add_argument("forward_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(normalize_argv(sys.argv[1:] if argv is None else argv))
    if args.command in {"train", "validate-full-video"} and args.forward_args[:1] == ["--"]:
        args.forward_args = args.forward_args[1:]
    return args


def _start_arguments(args: argparse.Namespace) -> list[str]:
    result = [
        "--host",
        args.host,
        "--backend-port",
        str(args.backend_port),
        "--frontend-port",
        str(args.frontend_port),
    ]
    if args.reload:
        result.append("--reload")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = normalize_argv(sys.argv[1:] if argv is None else argv)
    args = parse_args(raw_argv)
    root = repo_root()
    os.chdir(root)
    env = build_project_env(root)

    if args.command == "check":
        return check_project(root)

    python_exe = virtualenv_python(root)
    if not python_exe.is_file():
        print(f"[ERROR] Missing root virtual environment Python: {python_exe}", file=sys.stderr)
        return 1
    if not _same_executable(Path(sys.executable), python_exe):
        return restart_in_virtualenv(python_exe, raw_argv, env, root=root)

    if args.command == "start":
        return run_script(
            python_exe,
            root / "python_backend" / "scripts" / "start_ui.py",
            _start_arguments(args),
            root=root,
            env=env,
        )
    if args.command in {"stop", "status"}:
        return run_script(
            python_exe,
            root / "python_backend" / "scripts" / "start_ui.py",
            [f"--{args.command}"],
            root=root,
            env=env,
        )
    if args.command == "test":
        return run_tests(
            root,
            python_exe,
            env,
            python_only=args.python_only,
            node_only=args.node_only,
            pattern=args.pattern,
        )
    if args.command == "train":
        return run_script(
            python_exe,
            root / "python_backend" / "scripts" / "train_candidate_classifier.py",
            args.forward_args,
            root=root,
            env=env,
        )
    if args.command == "validate-full-video":
        return run_script(
            python_exe,
            root / "python_backend" / "scripts" / "validate_broadcast_run.py",
            args.forward_args,
            root=root,
            env=env,
        )
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
