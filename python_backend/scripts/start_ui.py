from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_BACKEND_PORT = 8000
DEFAULT_FRONTEND_PORT = 5173
PORT_SEARCH_SPAN = 20
HTTP_READY_TIMEOUT_SECONDS = 45.0
BACKEND_STARTUP_TIMEOUT_SECONDS = 180.0
STATE_PATH = Path(".run") / "ui_processes.json"
LOG_DIR = Path(".run") / "ui"
STATE_SCHEMA_VERSION = 2
STATE_OWNERSHIP = "spawn-root-tree"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return state if isinstance(state, dict) else None


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(state, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def remove_state(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


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


def wait_for_http_ok(
    url: str,
    timeout_seconds: float = HTTP_READY_TIMEOUT_SECONDS,
    poll_interval: float = 0.5,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            with urllib.request.urlopen(url, timeout=remaining) as response:
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
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        return any(len(row) > 1 and row[1].strip() == str(pid) for row in csv.reader(result.stdout.splitlines()))
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def identities_match(pid: int, expected: object, current: object) -> bool:
    if pid <= 0 or not isinstance(expected, dict) or not isinstance(current, dict):
        return False
    expected_started = str(expected.get("started", ""))
    current_started = str(current.get("started", ""))
    if not expected_started or expected_started != current_started:
        return False
    expected_pid = str(expected.get("pid", pid))
    current_pid = str(current.get("pid", pid))
    if expected_pid != str(pid) or current_pid != str(pid):
        return False
    expected_kind = str(expected.get("identity_kind", ""))
    current_kind = str(current.get("identity_kind", ""))
    if expected_kind == "ps-lstart-command" or current_kind == "ps-lstart-command":
        return bool(expected.get("command")) and expected.get("command") == current.get("command")
    return True


def _identity_is_present(pid: int, expected_identity: object) -> bool:
    return identities_match(pid, expected_identity, process_identity(pid))


def _wait_for_identity_exit(pid: int, expected_identity: object, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _identity_is_present(pid, expected_identity):
            return True
        time.sleep(0.1)
    return not _identity_is_present(pid, expected_identity)


def terminate_process_tree(
    pid: int,
    expected_identity: object = None,
    timeout_seconds: float = 5.0,
) -> bool:
    if pid <= 0:
        return True
    if not isinstance(expected_identity, dict):
        return False
    if not _identity_is_present(pid, expected_identity):
        return True
    if os.name == "nt":
        if not _identity_is_present(pid, expected_identity):
            return True
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        return _wait_for_identity_exit(pid, expected_identity, timeout_seconds)

    process_group: int | None = None
    try:
        process_group = os.getpgid(pid)
        if not _identity_is_present(pid, expected_identity):
            return True
        os.killpg(process_group, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        if not _identity_is_present(pid, expected_identity):
            return True
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return not _identity_is_present(pid, expected_identity)
    if _wait_for_identity_exit(pid, expected_identity, timeout_seconds):
        return True

    try:
        if not _identity_is_present(pid, expected_identity):
            return True
        if process_group is not None:
            os.killpg(process_group, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except OSError:
        return not _identity_is_present(pid, expected_identity)
    return _wait_for_identity_exit(pid, expected_identity, min(timeout_seconds, 2.0))


def terminate_spawned_process(
    process: subprocess.Popen[bytes],
    expected_identity: object,
    timeout_seconds: float = 5.0,
) -> bool:
    if isinstance(expected_identity, dict):
        return terminate_process_tree(process.pid, expected_identity, timeout_seconds=timeout_seconds)
    if process.poll() is not None:
        return True
    try:
        process.terminate()
        process.wait(timeout=timeout_seconds)
        return True
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=min(timeout_seconds, 2.0))
            return True
        except subprocess.TimeoutExpired:
            return False
    except OSError:
        return process.poll() is not None


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
            local_address, state, pid_text = parts[1], parts[3].upper(), parts[4]
            local_port = local_address.rsplit(":", 1)[-1]
            if state == "LISTENING" and local_port == str(port) and pid_text.isdigit():
                return int(pid_text)
        return None

    lsof = shutil.which("lsof")
    if lsof:
        result = subprocess.run(
            [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.strip().isdigit():
                    return int(line.strip())

    ss = shutil.which("ss")
    if ss:
        result = subprocess.run(
            [ss, "-ltnp", f"sport = :{port}"],
            capture_output=True,
            text=True,
            check=False,
        )
        match = re.search(r"pid=(\d+)", result.stdout) if result.returncode == 0 else None
        if match:
            return int(match.group(1))
    return None


def process_identity(pid: int, platform_name: str | None = None) -> dict[str, str] | None:
    if pid <= 0:
        return None
    platform_name = platform_name or os.name
    if platform_name == "nt":
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell is None:
            return None
        script = (
            f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; "
            "if ($null -ne $p) { "
            "@{ command = [string]$p.CommandLine; started = $p.CreationDate.ToUniversalTime().ToString('o') } "
            "| ConvertTo-Json -Compress }"
        )
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            identity = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(identity, dict):
            return None
        return {
            "pid": str(pid),
            "command": str(identity.get("command", "")),
            "started": str(identity.get("started", "")),
            "identity_kind": "windows-creation-time",
        }

    proc_dir = Path("/proc") / str(pid)
    if proc_dir.exists():
        try:
            command = " ".join(
                item.decode(errors="replace") for item in (proc_dir / "cmdline").read_bytes().split(b"\0") if item
            )
            stat_tail = (proc_dir / "stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
            return {
                "pid": str(pid),
                "command": command,
                "started": stat_tail[19],
                "identity_kind": "proc-start-ticks",
            }
        except (OSError, IndexError):
            return None

    ps = shutil.which("ps")
    if ps is None:
        return None
    result = subprocess.run(
        [ps, "-p", str(pid), "-o", "lstart=", "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        return None
    return {
        "pid": str(pid),
        "command": value[24:].strip(),
        "started": value[:24],
        "identity_kind": "ps-lstart-command",
    }


def parent_process_id(pid: int, platform_name: str | None = None) -> int | None:
    if pid <= 0:
        return None
    platform_name = platform_name or os.name
    if platform_name == "nt":
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell is None:
            return None
        script = (
            f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; "
            "if ($null -ne $p) { [Console]::Write([string]$p.ParentProcessId) }"
        )
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
        )
        value = result.stdout.strip()
        return int(value) if result.returncode == 0 and value.isdigit() else None

    proc_dir = Path("/proc") / str(pid)
    if proc_dir.exists():
        try:
            stat_tail = (proc_dir / "stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
            return int(stat_tail[1])
        except (OSError, IndexError, ValueError):
            return None

    ps = shutil.which("ps")
    if ps is None:
        return None
    result = subprocess.run(
        [ps, "-p", str(pid), "-o", "ppid="],
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip()
    return int(value) if result.returncode == 0 and value.isdigit() else None


def is_process_descendant(pid: int, root_pid: int) -> bool:
    if pid <= 0 or root_pid <= 0:
        return False
    current = pid
    visited: set[int] = set()
    for _ in range(256):
        if current == root_pid:
            return True
        if current <= 1 or current in visited:
            return False
        visited.add(current)
        parent = parent_process_id(current)
        if parent is None:
            return False
        current = parent
    return False


def is_owned_managed_process(pid: int, identity: object) -> bool:
    return identities_match(pid, identity, process_identity(pid))


def is_expected_managed_process(pid: int, port: int | None, identity: object = None) -> bool:
    if pid <= 0 or not isinstance(port, int) or port <= 0 or not isinstance(identity, dict):
        return False
    if not is_owned_managed_process(pid, identity):
        return False
    listener_pid = listening_pid_for_port(port)
    return listener_pid is not None and is_process_descendant(listener_pid, pid)


def _remove_service_state(state: dict[str, Any], service: str) -> None:
    for key in tuple(state):
        if key.startswith(f"{service}_"):
            state.pop(key, None)


def _listener_ownership(state: dict[str, Any], service: str, *, legacy: bool = False) -> str:
    pid_key = f"{service}_pid" if legacy else f"{service}_listener_pid"
    identity_key = f"{service}_identity" if legacy else f"{service}_listener_identity"
    pid = state.get(pid_key)
    port = state.get(f"{service}_port")
    identity = state.get(identity_key)
    if not isinstance(pid, int) or not isinstance(port, int) or not isinstance(identity, dict):
        return "unproven"
    current_listener = listening_pid_for_port(port)
    if current_listener == pid and identities_match(pid, identity, process_identity(pid)):
        return "owned"
    if current_listener is None and not _identity_is_present(pid, identity):
        return "absent"
    return "unproven"


def terminate_startup_service(
    service: str,
    process: subprocess.Popen[bytes],
    root_identity: object,
    port: int,
    listener: tuple[int, dict[str, str]] | None,
) -> tuple[bool, str | None]:
    issues: list[str] = []
    if not terminate_spawned_process(process, root_identity):
        issues.append(f"{service} root PID {process.pid} could not be terminated")
    if listener is not None:
        probe_state = {
            f"{service}_port": port,
            f"{service}_listener_pid": listener[0],
            f"{service}_listener_identity": listener[1],
        }
        listener_status = _listener_ownership(probe_state, service)
        if listener_status == "owned":
            if not terminate_process_tree(listener[0], listener[1]):
                issues.append(f"captured {service} listener PID {listener[0]} could not be terminated")
            elif _listener_ownership(probe_state, service) != "absent":
                issues.append(f"captured {service} listener cleanup could not be verified")
        elif listener_status != "absent":
            issues.append(f"captured {service} listener ownership could not be proven")
    elif listening_pid_for_port(port) is not None:
        issues.append(f"unproven {service} listener remains on port {port}")
    return not issues, "; ".join(issues) if issues else None


def _legacy_port_stays_clear(port: int, grace_seconds: float = 1.0) -> bool:
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if listening_pid_for_port(port) is not None:
            return False
        time.sleep(0.1)
    return listening_pid_for_port(port) is None


def _cleanup_legacy_state(path: Path, state: dict[str, Any]) -> bool:
    remaining_state = dict(state)
    did_cleanup = False
    failures: list[str] = []
    for service in ("frontend", "backend"):
        if f"{service}_pid" not in state:
            continue
        ownership = _listener_ownership(state, service, legacy=True)
        if ownership == "absent":
            _remove_service_state(remaining_state, service)
            continue
        if ownership == "owned":
            pid = state[f"{service}_pid"]
            port = state[f"{service}_port"]
            identity = state[f"{service}_identity"]
            if terminate_process_tree(pid, identity):
                if service == "backend" and state.get("reload", True):
                    failures.append(
                        "legacy backend worker stopped, but reload parent ownership is unknown; state retained"
                    )
                    continue
                if _legacy_port_stays_clear(port):
                    _remove_service_state(remaining_state, service)
                    did_cleanup = True
                    continue
                failures.append(
                    f"legacy {service} listener respawned on port {port}; parent ownership is unknown and state retained"
                )
                continue
            failures.append(f"legacy {service} listener PID {pid} could not be terminated")
            continue
        failures.append(f"legacy state ownership could not be proven for {service}; state retained")

    if failures:
        diagnostic = "; ".join(failures)
        remaining_state["cleanup_diagnostic"] = diagnostic
        save_state(path, remaining_state)
        raise RuntimeError(diagnostic)
    remove_state(path)
    return did_cleanup


def cleanup_managed_processes(path: Path) -> bool:
    state = load_state(path)
    if not state:
        remove_state(path)
        return False
    schema_version = state.get("schema_version")
    ownership_mode = state.get("ownership")
    if schema_version is None:
        return _cleanup_legacy_state(path, state)
    if schema_version != STATE_SCHEMA_VERSION or ownership_mode != STATE_OWNERSHIP:
        diagnostic = "Unsupported managed UI state schema or ownership mode; state retained"
        state["cleanup_diagnostic"] = diagnostic
        save_state(path, state)
        raise RuntimeError(diagnostic)

    did_cleanup = False
    remaining_state = dict(state)
    failures: list[str] = []
    for service in ("frontend", "backend"):
        if f"{service}_pid" not in state:
            continue
        pid = state.get(f"{service}_pid")
        identity = state.get(f"{service}_identity")
        if isinstance(pid, int) and is_owned_managed_process(pid, identity):
            if not terminate_process_tree(pid, identity):
                failures.append(f"{service} root PID {pid} could not be terminated")
                continue
            did_cleanup = True
            listener_after_root = _listener_ownership(state, service)
            if listener_after_root == "absent":
                _remove_service_state(remaining_state, service)
                continue
            if listener_after_root == "owned":
                listener_pid = state[f"{service}_listener_pid"]
                listener_identity = state[f"{service}_listener_identity"]
                if terminate_process_tree(listener_pid, listener_identity):
                    _remove_service_state(remaining_state, service)
                else:
                    failures.append(f"orphaned {service} listener PID {listener_pid} could not be terminated")
                continue
            failures.append(f"ownership could not be proven after stopping {service} root; state retained")
            continue

        listener_ownership = _listener_ownership(state, service)
        if listener_ownership == "absent":
            _remove_service_state(remaining_state, service)
            continue
        if listener_ownership == "owned":
            listener_pid = state[f"{service}_listener_pid"]
            listener_identity = state[f"{service}_listener_identity"]
            if terminate_process_tree(listener_pid, listener_identity):
                _remove_service_state(remaining_state, service)
                did_cleanup = True
            else:
                failures.append(f"orphaned {service} listener PID {listener_pid} could not be terminated")
            continue
        failures.append(f"ownership could not be proven for orphaned {service}; state retained")

    if failures:
        diagnostic = "; ".join(failures)
        remaining_state["cleanup_diagnostic"] = diagnostic
        save_state(path, remaining_state)
        raise RuntimeError(diagnostic)
    remove_state(path)
    return did_cleanup


def build_backend_env(base_env: dict[str, str], root_dir: Path) -> dict[str, str]:
    env = dict(base_env)
    backend = str((root_dir / "python_backend").resolve())
    existing = [item for item in env.get("PYTHONPATH", "").split(os.pathsep) if item]
    normalized_backend = os.path.normcase(os.path.abspath(backend))
    existing = [item for item in existing if os.path.normcase(os.path.abspath(item)) != normalized_backend]
    existing.insert(0, backend)
    env["PYTHONPATH"] = os.pathsep.join(existing)
    return env


def build_frontend_env(base_env: dict[str, str], host: str, backend_port: int, frontend_port: int) -> dict[str, str]:
    env = dict(base_env)
    env["VITE_API_PROXY_TARGET"] = f"http://{host}:{backend_port}"
    env["BASE_PATH"] = env.get("BASE_PATH") or "/"
    env["PORT"] = str(frontend_port)
    env["FT_DEV_HOST"] = host
    env["FT_FRONTEND_PORT"] = str(frontend_port)
    env["FT_BACKEND_PORT"] = str(backend_port)
    return env


def resolve_pnpm_command() -> list[str] | None:
    pnpm = shutil.which("pnpm")
    if pnpm:
        return [pnpm]
    corepack = shutil.which("corepack")
    if corepack:
        return [corepack, "pnpm"]
    return None


def ensure_requirements(root_dir: Path, python_exe: Path) -> None:
    missing: list[str] = []
    if not python_exe.is_file():
        missing.append(str(python_exe))
    if not (root_dir / "artifacts" / "web" / "package.json").is_file():
        missing.append("artifacts/web/package.json")
    if not (root_dir / "node_modules").is_dir():
        missing.append("node_modules (run pnpm install)")
    if not (root_dir / "artifacts" / "web" / "node_modules" / "vite").is_dir():
        missing.append("artifacts/web/node_modules/vite (run pnpm install)")
    if resolve_pnpm_command() is None:
        missing.append("pnpm or Corepack")
    if missing:
        raise RuntimeError("Missing required local dependencies: " + ", ".join(missing))


def spawn_background(command: list[str], cwd: Path, env: dict[str, str], log_path: Path) -> subprocess.Popen[bytes]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    creationflags = 0
    start_new_session = os.name != "nt"
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with log_path.open("ab") as log_file:
        return subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )


def wait_for_process_identity(pid: int, timeout_seconds: float = 5.0) -> dict[str, str] | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        identity = process_identity(pid)
        if identity is not None:
            return identity
        time.sleep(0.1)
    return None


def wait_for_listener_identity(
    port: int,
    root_pid: int | None = None,
    root_identity: dict[str, str] | None = None,
    timeout_seconds: float = 5.0,
) -> tuple[int, dict[str, str]] | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if root_pid is not None and not is_owned_managed_process(root_pid, root_identity):
            return None
        pid = listening_pid_for_port(port)
        identity = process_identity(pid) if pid is not None else None
        owned = root_pid is None or (pid is not None and is_process_descendant(pid, root_pid))
        if pid is not None and identity is not None and owned:
            return pid, identity
        time.sleep(0.2)
    return None


def _listener_is_owned(
    listener: tuple[int, dict[str, str]],
    root_pid: int,
    root_identity: dict[str, str],
) -> bool:
    return is_owned_managed_process(root_pid, root_identity) and is_process_descendant(listener[0], root_pid)


def run_check(root_dir: Path, python_exe: Path) -> int:
    checks = {
        "root_virtualenv": python_exe.is_file(),
        "web_package": (root_dir / "artifacts" / "web" / "package.json").is_file(),
        "node_modules": (root_dir / "node_modules").is_dir(),
        "web_dependencies": (root_dir / "artifacts" / "web" / "node_modules" / "vite").is_dir(),
        "pnpm": resolve_pnpm_command() is not None,
    }
    for name, ready in checks.items():
        print(f"{name.upper()}={'OK' if ready else 'MISSING'}")
    return 0 if all(checks.values()) else 1


def start_ui(
    root_dir: Path,
    python_exe: Path,
    host: str,
    backend_port: int,
    frontend_port: int,
    reload_enabled: bool,
) -> int:
    state_path = root_dir / STATE_PATH
    ensure_requirements(root_dir, python_exe)
    if cleanup_managed_processes(state_path):
        print("[INFO] Stopped previously managed UI processes.")

    chosen_backend_port = find_available_port(backend_port, host=host)
    if chosen_backend_port != backend_port:
        print(f"[WARN] Backend port {backend_port} unavailable. Using {chosen_backend_port} instead.")

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
    backend_process = spawn_background(
        backend_command,
        root_dir,
        build_backend_env(os.environ.copy(), root_dir),
        root_dir / LOG_DIR / "backend.log",
    )

    frontend_process: subprocess.Popen[bytes] | None = None
    backend_root_identity: dict[str, str] | None = None
    frontend_root_identity: dict[str, str] | None = None
    backend_listener: tuple[int, dict[str, str]] | None = None
    frontend_listener: tuple[int, dict[str, str]] | None = None
    chosen_frontend_port: int | None = None
    try:
        backend_root_identity = wait_for_process_identity(backend_process.pid)
        if backend_root_identity is None:
            raise RuntimeError("Backend root process identity could not be verified.")
        backend_health_url = f"http://{host}:{chosen_backend_port}/api/v1/health"
        if not wait_for_http_ok(
            backend_health_url,
            timeout_seconds=BACKEND_STARTUP_TIMEOUT_SECONDS,
        ):
            raise RuntimeError(f"Backend failed health check. See {root_dir / LOG_DIR / 'backend.log'}")
        backend_listener_candidate = wait_for_listener_identity(
            chosen_backend_port,
            backend_process.pid,
            backend_root_identity,
        )
        if backend_listener_candidate is None:
            raise RuntimeError("Backend listener identity could not be verified.")
        if not _listener_is_owned(backend_listener_candidate, backend_process.pid, backend_root_identity):
            raise RuntimeError("Backend listener is not owned by the spawned backend process tree.")
        backend_listener = backend_listener_candidate

        chosen_frontend_port = find_available_port(frontend_port, host=host)
        if chosen_frontend_port != frontend_port:
            print(f"[WARN] Frontend port {frontend_port} unavailable. Using {chosen_frontend_port} instead.")
        base_pnpm = resolve_pnpm_command()
        if base_pnpm is None:
            raise RuntimeError("pnpm was not found.")
        frontend_command = [
            *base_pnpm,
            "--filter",
            "@workspace/web",
            "run",
            "dev",
        ]
        frontend_process = spawn_background(
            frontend_command,
            root_dir,
            build_frontend_env(os.environ.copy(), host, chosen_backend_port, chosen_frontend_port),
            root_dir / LOG_DIR / "frontend.log",
        )
        frontend_root_identity = wait_for_process_identity(frontend_process.pid)
        if frontend_root_identity is None:
            raise RuntimeError("Frontend root process identity could not be verified.")
        frontend_url = f"http://{host}:{chosen_frontend_port}/broadcast"
        if not wait_for_http_ok(
            frontend_url,
            timeout_seconds=HTTP_READY_TIMEOUT_SECONDS,
        ):
            raise RuntimeError(f"Frontend failed route check. See {root_dir / LOG_DIR / 'frontend.log'}")
        frontend_api_health_url = f"http://{host}:{chosen_frontend_port}/api/healthz"
        if not wait_for_http_ok(
            frontend_api_health_url,
            timeout_seconds=HTTP_READY_TIMEOUT_SECONDS,
        ):
            raise RuntimeError(f"Frontend API proxy failed health check. See {root_dir / LOG_DIR / 'frontend.log'}")
        frontend_listener_candidate = wait_for_listener_identity(
            chosen_frontend_port,
            frontend_process.pid,
            frontend_root_identity,
        )
        if frontend_listener_candidate is None:
            raise RuntimeError("Frontend listener identity could not be verified.")
        if not _listener_is_owned(frontend_listener_candidate, frontend_process.pid, frontend_root_identity):
            raise RuntimeError("Frontend listener is not owned by the spawned frontend process tree.")
        frontend_listener = frontend_listener_candidate

        state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "ownership": STATE_OWNERSHIP,
            "backend_pid": backend_process.pid,
            "backend_port": chosen_backend_port,
            "backend_identity": backend_root_identity,
            "backend_listener_pid": backend_listener[0],
            "backend_listener_identity": backend_listener[1],
            "backend_url": f"http://{host}:{chosen_backend_port}",
            "frontend_pid": frontend_process.pid,
            "frontend_port": chosen_frontend_port,
            "frontend_identity": frontend_root_identity,
            "frontend_listener_pid": frontend_listener[0],
            "frontend_listener_identity": frontend_listener[1],
            "frontend_url": f"http://{host}:{chosen_frontend_port}",
            "host": host,
            "reload": reload_enabled,
        }
        save_state(state_path, state)
        print(f"[INFO] Backend: {state['backend_url']}")
        print(f"[INFO] Broadcast UI: {frontend_url}")
        print(f"[INFO] Logs: {root_dir / LOG_DIR}")
        return 0
    except Exception as error:
        failed_state: dict[str, Any] = {
            "schema_version": STATE_SCHEMA_VERSION,
            "ownership": STATE_OWNERSHIP,
            "host": host,
            "reload": reload_enabled,
        }
        termination_failures: list[str] = []
        if frontend_process is not None and chosen_frontend_port is not None:
            frontend_stopped, frontend_diagnostic = terminate_startup_service(
                "frontend",
                frontend_process,
                frontend_root_identity,
                chosen_frontend_port,
                frontend_listener,
            )
            if not frontend_stopped:
                termination_failures.append(frontend_diagnostic or "frontend cleanup failed")
                failed_state.update(
                    {
                        "frontend_pid": frontend_process.pid,
                        "frontend_port": chosen_frontend_port,
                        "frontend_identity": frontend_root_identity,
                    }
                )
                if frontend_listener is not None:
                    failed_state.update(
                        {
                            "frontend_listener_pid": frontend_listener[0],
                            "frontend_listener_identity": frontend_listener[1],
                        }
                    )
        backend_stopped, backend_diagnostic = terminate_startup_service(
            "backend",
            backend_process,
            backend_root_identity,
            chosen_backend_port,
            backend_listener,
        )
        if not backend_stopped:
            termination_failures.append(backend_diagnostic or "backend cleanup failed")
            failed_state.update(
                {
                    "backend_pid": backend_process.pid,
                    "backend_port": chosen_backend_port,
                    "backend_identity": backend_root_identity,
                }
            )
            if backend_listener is not None:
                failed_state.update(
                    {
                        "backend_listener_pid": backend_listener[0],
                        "backend_listener_identity": backend_listener[1],
                    }
                )
        if termination_failures:
            failed_state["cleanup_diagnostic"] = "Startup cleanup could not terminate: " + "; ".join(
                termination_failures
            )
            save_state(state_path, failed_state)
            raise RuntimeError(
                f"{error} Startup cleanup could not terminate: " + "; ".join(termination_failures)
            ) from error
        remove_state(state_path)
        raise


def stop_ui(root_dir: Path) -> int:
    state_path = root_dir / STATE_PATH
    try:
        if cleanup_managed_processes(state_path):
            print("[INFO] Stopped managed UI processes.")
        else:
            print("[INFO] No owned managed UI processes were running.")
        return 0
    except RuntimeError as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 1


def status_ui(root_dir: Path) -> int:
    state = load_state(root_dir / STATE_PATH)
    if not state:
        print("[INFO] Managed UI is stopped.")
        return 1
    if state.get("schema_version") != STATE_SCHEMA_VERSION or state.get("ownership") != STATE_OWNERSHIP:
        label = "LEGACY" if state.get("schema_version") is None else "UNSUPPORTED"
        print(f"[WARN] Managed UI state is {label}; run --stop for identity-safe cleanup.")
        return 1
    ready = True
    for service in ("backend", "frontend"):
        pid = state.get(f"{service}_pid")
        port = state.get(f"{service}_port")
        identity = state.get(f"{service}_identity")
        running = isinstance(pid, int) and is_expected_managed_process(pid, port, identity)
        if running:
            status = "RUNNING"
        elif _listener_ownership(state, service) == "owned":
            status = "ORPHANED"
        else:
            status = "STALE"
        print(f"{service.upper()}={status}")
        ready = ready and running
    return 0 if ready else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Managed local UI launcher for football tracking.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Print environment readiness info.")
    mode.add_argument("--stop", action="store_true", help="Stop previously managed UI processes.")
    mode.add_argument("--status", action="store_true", help="Check managed UI process identities and ports.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host to bind both services to.")
    parser.add_argument("--backend-port", type=int, default=DEFAULT_BACKEND_PORT, help="Preferred backend port.")
    parser.add_argument("--frontend-port", type=int, default=DEFAULT_FRONTEND_PORT, help="Preferred frontend port.")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn reload mode.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root_dir = repo_root()
    python_exe = root_dir / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    if args.check:
        return run_check(root_dir, python_exe)
    if args.stop:
        return stop_ui(root_dir)
    if args.status:
        return status_ui(root_dir)

    try:
        return start_ui(
            root_dir=root_dir,
            python_exe=python_exe,
            host=args.host,
            backend_port=args.backend_port,
            frontend_port=args.frontend_port,
            reload_enabled=args.reload,
        )
    except RuntimeError as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
