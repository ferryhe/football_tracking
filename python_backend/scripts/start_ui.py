from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_BACKEND_PORT = 8000
DEFAULT_FRONTEND_PORT = 5173
PORT_SEARCH_SPAN = 20
STATE_PATH = Path(".run") / "ui_processes.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def remove_state(path: Path) -> None:
    if path.exists():
        path.unlink()


def is_port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def find_available_port(preferred_port: int, host: str = DEFAULT_HOST, search_span: int = PORT_SEARCH_SPAN) -> int:
    for port in range(preferred_port, preferred_port + search_span + 1):
        if is_port_available(host, port):
            return port
    raise RuntimeError(f"No available port found starting at {preferred_port}.")


def wait_for_http_ok(url: str, timeout_seconds: float = 45.0, poll_interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            time.sleep(poll_interval)
    return False


def is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate_process_tree(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        return
    try:
        os.kill(pid, 15)
    except OSError:
        return


def listening_pid_for_port(port: int) -> int | None:
    if port <= 0:
        return None
    if os.name == "nt":
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        for raw_line in result.stdout.splitlines():
            parts = raw_line.split()
            if len(parts) < 5 or parts[0].upper() != "TCP":
                continue
            local_address = parts[1]
            state = parts[3].upper()
            pid_text = parts[4]
            if state != "LISTENING" or not local_address.endswith(f":{port}") or not pid_text.isdigit():
                continue
            return int(pid_text)
        return None

    if shutil.which("lsof") is None:
        return None
    result = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        candidate = line.strip()
        if candidate.isdigit():
            return int(candidate)
    return None


def is_expected_managed_process(pid: int, port: int | None) -> bool:
    if pid <= 0 or not isinstance(port, int) or port <= 0:
        return False
    return listening_pid_for_port(port) == pid


def cleanup_managed_processes(path: Path) -> bool:
    state = load_state(path)
    if not state:
        remove_state(path)
        return False

    did_cleanup = False
    for pid_key, port_key in (("frontend_pid", "frontend_port"), ("backend_pid", "backend_port")):
        pid = state.get(pid_key)
        port = state.get(port_key)
        if isinstance(pid, int) and is_process_running(pid) and is_expected_managed_process(pid, port):
            terminate_process_tree(pid)
            did_cleanup = True

    remove_state(path)
    return did_cleanup


def build_frontend_env(base_env: dict[str, str], host: str, backend_port: int, frontend_port: int) -> dict[str, str]:
    env = dict(base_env)
    env["VITE_API_PROXY_TARGET"] = f"http://{host}:{backend_port}"
    env["FT_DEV_HOST"] = host
    env["FT_FRONTEND_PORT"] = str(frontend_port)
    env["FT_BACKEND_PORT"] = str(backend_port)
    return env


def ensure_requirements(root_dir: Path, python_exe: Path) -> None:
    if not python_exe.exists():
        raise RuntimeError(f"Missing virtual environment Python: {python_exe}")
    if not (root_dir / "frontend" / "package.json").exists():
        raise RuntimeError("Missing frontend/package.json")
    if shutil.which("npm") is None:
        raise RuntimeError("npm was not found in PATH.")


def ensure_frontend_dependencies(frontend_dir: Path) -> None:
    node_modules_dir = frontend_dir / "node_modules"
    if node_modules_dir.exists():
        return
    print("[INFO] frontend/node_modules not found. Installing frontend dependencies...")
    completed = subprocess.run(["npm", "install"], cwd=str(frontend_dir), check=False)
    if completed.returncode != 0:
        raise RuntimeError("npm install failed.")


def spawn_console(title: str, command: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.Popen[str]:
    cmdline = subprocess.list2cmdline(command)
    shell_command = f"title {title} & {cmdline}"
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    return subprocess.Popen(
        ["cmd", "/k", shell_command],
        cwd=str(cwd),
        env=env,
        creationflags=creationflags,
        text=True,
    )


def run_check(root_dir: Path, python_exe: Path) -> int:
    print(f"ROOT_DIR={root_dir}")
    print(f"PYTHON_EXE={python_exe}")
    print(f"PYTHON_OK={1 if python_exe.exists() else 0}")
    print(f"FRONTEND_OK={1 if (root_dir / 'frontend' / 'package.json').exists() else 0}")
    print(f"NPM_OK={1 if shutil.which('npm') else 0}")
    return 0


def start_ui(root_dir: Path, python_exe: Path, host: str, backend_port: int, frontend_port: int, reload_enabled: bool) -> int:
    state_path = root_dir / STATE_PATH
    frontend_dir = root_dir / "frontend"

    ensure_requirements(root_dir, python_exe)
    ensure_frontend_dependencies(frontend_dir)

    if cleanup_managed_processes(state_path):
        print("[INFO] Stopped previously managed UI processes.")

    chosen_backend_port = find_available_port(backend_port, host=host)
    chosen_frontend_port = find_available_port(frontend_port, host=host)

    if chosen_backend_port != backend_port:
        print(f"[WARN] Backend port {backend_port} unavailable. Using {chosen_backend_port} instead.")
    if chosen_frontend_port != frontend_port:
        print(f"[WARN] Frontend port {frontend_port} unavailable. Using {chosen_frontend_port} instead.")

    backend_command = [
        str(python_exe),
        "-m",
        "uvicorn",
        "football_tracking.api.app:app",
        "--host",
        host,
        "--port",
        str(chosen_backend_port),
    ]
    if reload_enabled:
        backend_command.append("--reload")

    print("[INFO] Starting backend window...")
    backend_process = spawn_console("Football Tracking API", backend_command, root_dir)
    backend_health_url = f"http://{host}:{chosen_backend_port}/api/v1/health"

    print("[INFO] Waiting for backend health endpoint...")
    if not wait_for_http_ok(backend_health_url):
        terminate_process_tree(backend_process.pid)
        raise RuntimeError("Backend failed to start correctly. Check the backend window for details.")

    frontend_command = ["npm", "run", "dev", "--", "--host", host, "--port", str(chosen_frontend_port)]
    frontend_env = build_frontend_env(os.environ.copy(), host, chosen_backend_port, chosen_frontend_port)

    print("[INFO] Starting frontend window...")
    frontend_process = spawn_console("Football Tracking Frontend", frontend_command, frontend_dir, env=frontend_env)

    state = {
        "backend_pid": backend_process.pid,
        "backend_port": chosen_backend_port,
        "backend_url": f"http://{host}:{chosen_backend_port}",
        "frontend_pid": frontend_process.pid,
        "frontend_port": chosen_frontend_port,
        "frontend_url": f"http://{host}:{chosen_frontend_port}",
        "host": host,
        "reload": reload_enabled,
    }
    save_state(state_path, state)

    print(f"[INFO] Backend: {state['backend_url']}")
    print(f"[INFO] Frontend: {state['frontend_url']}")
    return 0


def stop_ui(root_dir: Path) -> int:
    state_path = root_dir / STATE_PATH
    if cleanup_managed_processes(state_path):
        print("[INFO] Stopped managed UI processes.")
    else:
        print("[INFO] No managed UI processes were running.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Managed local UI launcher for football tracking.")
    parser.add_argument("--check", action="store_true", help="Print environment readiness info.")
    parser.add_argument("--stop", action="store_true", help="Stop previously managed UI processes.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Loopback host to bind both services to.")
    parser.add_argument("--backend-port", type=int, default=DEFAULT_BACKEND_PORT, help="Preferred backend port.")
    parser.add_argument("--frontend-port", type=int, default=DEFAULT_FRONTEND_PORT, help="Preferred frontend port.")
    parser.add_argument("--no-reload", action="store_true", help="Disable uvicorn reload mode for a steadier backend.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root_dir = repo_root()
    python_exe = root_dir / ".venv" / "Scripts" / "python.exe"

    if args.check:
        return run_check(root_dir, python_exe)
    if args.stop:
        return stop_ui(root_dir)

    try:
        return start_ui(
            root_dir=root_dir,
            python_exe=python_exe,
            host=args.host,
            backend_port=args.backend_port,
            frontend_port=args.frontend_port,
            reload_enabled=not args.no_reload,
        )
    except RuntimeError as error:
        print(f"[ERROR] {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
