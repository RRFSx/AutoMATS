#!/usr/bin/env python3
"""
ssh_helper.py: Multiplex SSH & Persistent Background Terminal Helper for AutoMATS.

Workflow:
  1. Reads default RDHPCS usernames by cluster from `.hpc_config` (simple
     `<cluster>: <username>` mapping), if present.
  2. Detects whether an active multiplex SSH session (ControlMaster socket)
     already exists for the target HPC cluster (gaeac6, gaeac7, ursa, hera).
     - If YES: Reuses the active multiplex session immediately.
     - If NO:  Uses the configured/CLI username (or prompts if missing) and lets
               the user press their YubiKey or approve Duo Push to establish the
               multiplex SSH session, then saves `<cluster>: <username>` to `.hpc_config`.
  3. Starts/reuses a persistent background terminal session connected to the
     HPC headnode (handling Gaea bastion node selection automatically) that
     stays alive permanently (until network/cluster disconnect or --stop) and
     executes arbitrary remote commands, including streaming binary `tar`.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import glob
import gzip
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Auto-re-exec with local venv python if invoked by system python
ROOT = Path(__file__).resolve().parent.parent
venv_dir = ROOT / ".venv"
venv_python = venv_dir / "bin" / "python"
if __name__ == "__main__" and venv_python.is_file() and Path(sys.prefix).resolve() != venv_dir.resolve():
    try:
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve())] + sys.argv[1:])
    except OSError:
        pass

# Cluster endpoint configurations (priority ordered)
HPC_ENDPOINTS: Dict[str, List[str]] = {
    "gaeac6": [
        "gaea-c6.princeton.rdhpcs.noaa.gov",
        "gaea-c6.fairmont.rdhpcs.noaa.gov",
    ],
    "gaeac7": [
        "gaea-c7.princeton.rdhpcs.noaa.gov",
        "gaea-c7.fairmont.rdhpcs.noaa.gov",
    ],
    "ursa": [
        "ursa-mfa.fairmont.rdhpcs.noaa.gov",
        "ursa-mfa.princeton.rdhpcs.noaa.gov",
    ],
    "hera": [
        "hera-mfa.fairmont.rdhpcs.noaa.gov",
        "hera-mfa.princeton.rdhpcs.noaa.gov",
    ],
}

DEFAULT_HEAD_NODES: Dict[str, str] = {
    "gaeac5": "gaea51",
    "gaeac6": "gaea61",
    "gaeac7": "gaea71",
    "ursa": "ufe04",
    "hera": "hfe01",
}

DEFAULT_IDLE_TIMEOUT = 0  # 0 = keep alive permanently until connection lost or --stop

PROMPT_REGEX = re.compile(
    r"\[.*?@?[\w\.\-]+.*?\]\s*[\$#>%]\s*$|[\w\.\-]+[^\n\r]*[\$#>%]\s*$|[\$#>%]\s*$"
)

SSH_CONFIG_BLOCK = """Host *  !*github.com
    ServerAliveInterval 60
    ControlMaster auto
    ControlPath ~/.ssh/master-%r@%h:%p
    ControlPersist yes
"""


# ==============================================================================
# Paths & SSH Configuration Management
# ==============================================================================


def get_default_config_path() -> Path:
    """Finds .hpc_config path in CWD or AutoMATS root directory."""
    cwd_cfg = Path.cwd() / ".hpc_config"
    if cwd_cfg.exists():
        return cwd_cfg

    automats_cfg = ROOT / ".hpc_config"
    if automats_cfg.exists():
        return automats_cfg

    return automats_cfg if ROOT.exists() else cwd_cfg


def _clean_config_username(value: Any) -> Optional[str]:
    """Normalizes a configured username string, returning None if unset or placeholder."""
    if value is None:
        return None
    val = str(value).strip().strip("'\"")
    if not val or val.lower() in ("null", "none", "~", "<your_username>"):
        return None
    return val


def load_hpc_config(config_path: Optional[Path] = None) -> Dict[str, str]:
    """
    Loads simple `<cluster>: <username>` mappings from `.hpc_config`.
    Also tolerates legacy nested `<cluster>:\\n  username: <username>` blocks.
    """
    path = config_path or get_default_config_path()
    if not path.is_file():
        return {}

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    result: Dict[str, str] = {}
    current_section: Optional[str] = None
    for raw in text.splitlines():
        no_comment = re.sub(r"(?:^|\s)#.*$", "", raw).rstrip()
        stripped = no_comment.strip()
        if not stripped or ":" not in stripped:
            continue

        indent = len(no_comment) - len(no_comment.lstrip(" \t"))
        k, v = stripped.split(":", 1)
        key = k.strip().strip("'\"").lower()
        if not key:
            continue

        if indent == 0:
            user = _clean_config_username(v)
            if v.strip():
                current_section = None
                if user:
                    result[key] = user
            else:
                current_section = key
        elif current_section and key in ("username", "user"):
            user = _clean_config_username(v)
            if user:
                result[current_section] = user

    return result


def save_hpc_config(config_path: Path, config: Dict[str, str]) -> None:
    """Writes simple `<cluster>: <username>` mappings to `.hpc_config`."""
    cleaned: Dict[str, str] = {}
    for k, v in config.items():
        ck = str(k).strip().lower()
        cv = _clean_config_username(v)
        if ck and cv:
            cleaned[ck] = cv

    ordered_keys = [k for k in HPC_ENDPOINTS.keys() if k in cleaned]
    ordered_keys.extend(sorted(k for k in cleaned if k not in HPC_ENDPOINTS))

    lines = ["# RDHPCS usernames by cluster"]
    lines.extend(f"{k}: {cleaned[k]}" for k in ordered_keys)
    content = "\n".join(lines) + "\n"

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(content, encoding="utf-8")
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass


def update_hpc_config(config_path: Path, hpc: str, username: str) -> None:
    """Records or updates `<hpc>: <username>` in `.hpc_config` if changed."""
    hpc_key = hpc.strip().lower()
    clean_user = _clean_config_username(username)
    if not hpc_key or not clean_user:
        return

    existing = load_hpc_config(config_path)
    if existing.get(hpc_key) == clean_user:
        return

    existing[hpc_key] = clean_user
    save_hpc_config(config_path, existing)


def get_worker_sock_path(user: str, cluster_name: str) -> str:
    """Path to the persistent background terminal worker UNIX socket."""
    return f"/tmp/hpc_term_{user}_{cluster_name.lower()}.sock"


def get_worker_pid_path(sock_path: str) -> str:
    """Path to the background worker daemon PID sidecar file."""
    return f"{sock_path}.pid"


def get_worker_err_path(sock_path: str) -> str:
    """Path to the background worker daemon startup error sidecar file."""
    return f"{sock_path}.err"


def get_master_sock_path(user: str, host: str, port: int = 22) -> Path:
    """Path to the OpenSSH ControlMaster UNIX socket."""
    return Path.home() / ".ssh" / f"master-{user}@{host}:{port}"


def is_bastion_host(host: str) -> bool:
    """Returns True if the host is a NOAA RDHPCS bastion/gateway."""
    low = host.lower()
    return ".rdhpcs.noaa.gov" in low or "-mfa." in low or "-cac." in low or "bastion" in low


def ensure_ssh_config(config_path: Optional[Path] = None) -> bool:
    """Ensures SSH ControlMaster directives exist in ~/.ssh/config."""
    ssh_dir = Path.home() / ".ssh"
    config_file = config_path or (ssh_dir / "config")

    ssh_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(ssh_dir, 0o700)
    except OSError:
        pass

    if not config_file.exists():
        config_file.write_text(SSH_CONFIG_BLOCK, encoding="utf-8")
        try:
            os.chmod(config_file, 0o600)
        except OSError:
            pass
        return True

    content = config_file.read_text(encoding="utf-8")
    has_control_path = bool(re.search(r"ControlPath\s+~/\.ssh/master-%r@%h:%p", content))
    has_control_master = bool(re.search(r"ControlMaster\s+auto", content))
    if has_control_path and has_control_master:
        return False

    separator = "\n" if content and not content.endswith("\n") else ""
    config_file.write_text(content + separator + SSH_CONFIG_BLOCK, encoding="utf-8")
    try:
        os.chmod(config_file, 0o600)
    except OSError:
        pass
    return True


# ==============================================================================
# Socket & Multiplex SSH Session Detection
# ==============================================================================


def is_socket_alive(socket_path: str) -> bool:
    """Checks whether a UNIX domain socket is active and accepting connections."""
    if not socket_path or not os.path.exists(socket_path):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(socket_path)
            return True
    except OSError:
        return False


def ping_worker_session(
    socket_path: str,
    timeout: float = 4.0,
    probe_remote: bool = False,
) -> bool:
    """Verifies that a worker daemon socket is responsive (and optionally probes remote shell)."""
    if not socket_path or not os.path.exists(socket_path):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(socket_path)
            payload = {"cmd": "ping", "probe_remote": bool(probe_remote)}
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            raw = bytearray()
            while b"\n" not in raw:
                chunk = s.recv(4096)
                if not chunk:
                    break
                raw.extend(chunk)
        if not raw:
            return False
        resp = json.loads(raw.decode("utf-8", errors="ignore").splitlines()[0].strip())
        return int(resp.get("code", 1)) == 0
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return False


def find_active_worker_session(
    hpc: str,
    preferred_username: Optional[str] = None,
    exact_user: bool = False,
    verify_ping: bool = True,
    probe_remote: bool = False,
) -> Optional[Tuple[str, str]]:
    """
    Finds an active persistent background terminal worker socket for `hpc`.
    Returns (username, worker_sock_path) if active, else None.
    """
    hpc_key = hpc.lower()
    candidates: List[str] = []
    if preferred_username and preferred_username != "<your_username>":
        candidates.append(get_worker_sock_path(preferred_username, hpc_key))
    if not (exact_user and preferred_username and preferred_username != "<your_username>"):
        candidates.extend(sorted(glob.glob(f"/tmp/hpc_term_*_{hpc_key}.sock")))

    seen: set[str] = set()
    suffix = f"_{hpc_key}.sock"
    for sock_str in candidates:
        if sock_str in seen:
            continue
        seen.add(sock_str)
        base = os.path.basename(sock_str)
        if not (base.startswith("hpc_term_") and base.endswith(suffix)):
            continue
        user = base[len("hpc_term_") : -len(suffix)]
        if not user:
            continue
        if verify_ping:
            if ping_worker_session(
                sock_str,
                timeout=4.5 if probe_remote else 2.0,
                probe_remote=probe_remote,
            ):
                return user, sock_str
        elif is_socket_alive(sock_str):
            return user, sock_str
    return None


def is_master_alive(
    username: str,
    host: str,
    port: int = 22,
    sock_path: Optional[Path] = None,
    verify_ssh_ctl: bool = True,
) -> bool:
    """Checks whether an active ControlMaster socket exists and responds."""
    master_sock = sock_path or get_master_sock_path(username, host, port)
    if not is_socket_alive(str(master_sock)):
        return False
    if not verify_ssh_ctl:
        return True
    try:
        res = subprocess.run(
            ["ssh", "-O", "check", "-S", str(master_sock), f"{username}@{host}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
        return res.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def cleanup_stale_master(username: str, host: str, port: int = 22) -> None:
    """Removes a dead/stale ControlMaster session and socket if present."""
    master_sock = get_master_sock_path(username, host, port)
    if master_sock.exists() and not is_master_alive(username, host, port, master_sock):
        close_control_master(username, host, sock_path=master_sock)


def _find_master_mux_pids(master_sock: Path) -> List[int]:
    """Finds local OpenSSH ControlMaster mux PIDs bound to `master_sock`."""
    sock_variants = {str(master_sock)}
    try:
        sock_variants.add(str(master_sock.resolve()))
    except OSError:
        pass

    pids: List[int] = []
    seen: set[int] = set()
    try:
        res = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (subprocess.SubprocessError, OSError):
        return pids

    if res.returncode != 0:
        return pids

    for raw_line in res.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid <= 0 or pid == os.getpid() or pid in seen:
            continue

        cmd_str = parts[1].strip()
        tokens = cmd_str.split()
        is_mux = any(
            cmd_str == f"ssh: {sv} [mux]" or (cmd_str.startswith("ssh:") and sv in tokens and "[mux]" in tokens)
            for sv in sock_variants
        )
        if not is_mux:
            exe = os.path.basename(tokens[0]) if tokens else ""
            if exe == "ssh" and "ControlMaster=yes" in tokens:
                has_ctrl_path = any(f"ControlPath={sv}" in tokens for sv in sock_variants)
                if not has_ctrl_path and "-S" in tokens:
                    for idx, tok in enumerate(tokens[:-1]):
                        if tok == "-S" and tokens[idx + 1] in sock_variants:
                            has_ctrl_path = True
                            break
                is_mux = has_ctrl_path

        if is_mux:
            seen.add(pid)
            pids.append(pid)

    return pids


def close_control_master(
    username: str,
    host: str,
    sock_path: Optional[Path] = None,
) -> None:
    """Cleanly exits, terminates if hung, and unlinks a ControlMaster session."""
    master_sock = sock_path or get_master_sock_path(username, host, 22)
    try:
        subprocess.run(
            ["ssh", "-O", "exit", "-S", str(master_sock), f"{username}@{host}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass

    for mpid in _find_master_mux_pids(master_sock):
        if _is_pid_running(mpid):
            _terminate_pid(mpid, signal.SIGTERM)
            wait_until = time.monotonic() + 0.5
            while time.monotonic() < wait_until and _is_pid_running(mpid):
                time.sleep(0.05)
            if _is_pid_running(mpid):
                _terminate_pid(mpid, signal.SIGKILL)

    try:
        if master_sock.exists():
            master_sock.unlink()
    except OSError:
        pass


def find_active_control_master(
    hpc: str,
    preferred_username: Optional[str] = None,
    verify_ssh_ctl: bool = True,
    exact_user: bool = False,
) -> Optional[Tuple[str, str, Path]]:
    """
    Detects whether an active multiplex SSH session already exists for `hpc`.
    Scans ~/.ssh/master-*@<endpoint>:22 across all endpoints for `hpc`.
    Automatically cleans up dead/unresponsive ControlMaster sockets and mux processes.
    Returns (username, endpoint, socket_path) if found, else None.
    """
    ssh_dir = Path.home() / ".ssh"
    if not ssh_dir.exists():
        return None

    endpoints = HPC_ENDPOINTS.get(hpc.lower(), [])
    for host in endpoints:
        candidates: List[str] = []
        if preferred_username and preferred_username != "<your_username>":
            candidates.append(str(ssh_dir / f"master-{preferred_username}@{host}:22"))
            if exact_user:
                candidates.extend(sorted(glob.glob(str(ssh_dir / f"master-{preferred_username}@{host}:*"))))
        if not (exact_user and preferred_username and preferred_username != "<your_username>"):
            candidates.extend(sorted(glob.glob(str(ssh_dir / f"master-*@{host}:*"))))

        seen: set[str] = set()
        for sock_str in candidates:
            if sock_str in seen:
                continue
            seen.add(sock_str)
            sock_path = Path(sock_str)
            if not sock_path.exists():
                continue

            base = sock_path.name
            m = re.match(r"^master-(.+?)@(.+?):(\d+)$", base)
            if not m:
                continue
            user = m.group(1)
            port = int(m.group(3))
            if not user or user == "*":
                continue

            if is_master_alive(user, host, port, sock_path=sock_path, verify_ssh_ctl=verify_ssh_ctl):
                return user, host, sock_path

            close_control_master(user, host, sock_path=sock_path)

    return None


def prompt_username(hpc: str, default_username: Optional[str] = None) -> Optional[str]:
    """Prompts user interactively to enter their RDHPCS username."""
    clean_default = (
        default_username
        if default_username and default_username != "<your_username>"
        else None
    )
    prompt_str = (
        f"Enter RDHPCS username for '{hpc}' [{clean_default}]: "
        if clean_default
        else f"Enter RDHPCS username for '{hpc}': "
    )
    try:
        entered = input(prompt_str).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None

    if entered:
        return entered
    return clean_default


def is_port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    """Tests TCP reachability to host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def select_working_address(hpc: str) -> str:
    """Probes cluster endpoints in priority order and returns the first reachable host."""
    hpc_key = hpc.lower()
    endpoints = HPC_ENDPOINTS.get(hpc_key)
    if not endpoints:
        raise ValueError(
            f"Unsupported HPC cluster '{hpc}'. Supported options: {list(HPC_ENDPOINTS.keys())}"
        )

    print(f"[*] Checking '{hpc}' endpoints...")
    for idx, addr in enumerate(endpoints, 1):
        print(f"    [{idx}/{len(endpoints)}] {addr}:22 ... ", end="", flush=True)
        if is_port_open(addr, 22, timeout=3.0):
            print("ONLINE")
            return addr
        print("UNREACHABLE")

    raise ConnectionError(
        f"Unable to reach any endpoint for cluster '{hpc}': {endpoints}."
    )


def establish_master_connection(
    username: str,
    hpc_address: str,
    hpc: str,
) -> Tuple[bool, Path]:
    """
    Establishes a multiplex SSH ControlMaster session to `username@hpc_address`.
    Allows the user to press YubiKey / click Duo Push and enter PIN/password.
    """
    master_sock = get_master_sock_path(username, hpc_address, 22)
    if is_master_alive(username, hpc_address, 22, sock_path=master_sock):
        return True, master_sock

    cleanup_stale_master(username, hpc_address, 22)
    ensure_ssh_config()

    print("\n" + "=" * 70)
    print(f"[*] ESTABLISHING MULTIPLEX SSH SESSION ({hpc.upper()})")
    print("=" * 70)
    print(f"Target: {username}@{hpc_address}")
    print("[!] Press your YubiKey / approve Duo Push (and enter PIN/password if prompted).")
    print("-" * 70 + "\n")

    cmd = [
        "ssh",
        "-o", "ControlMaster=yes",
        "-o", f"ControlPath={master_sock}",
        "-o", "ControlPersist=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=6",
        "-N", "-f",
        f"{username}@{hpc_address}",
    ]
    try:
        proc = subprocess.run(cmd)
        if proc.returncode != 0:
            print(f"[!] SSH authentication failed (exit code {proc.returncode}).", file=sys.stderr)
            return False, master_sock
    except KeyboardInterrupt:
        print("\n[!] Aborted by user.", file=sys.stderr)
        return False, master_sock

    for _ in range(10):
        if is_master_alive(username, hpc_address, 22, sock_path=master_sock):
            print(f"[✓] Multiplex SSH session established: {master_sock}")
            return True, master_sock
        time.sleep(0.2)

    print(f"[!] ControlMaster socket was not detected at {master_sock}.", file=sys.stderr)
    return False, master_sock


# ==============================================================================
# Persistent Background Terminal Session (Supports Commands & Streaming tar)
# ==============================================================================


class ClientDisconnectedError(Exception):
    """Raised when the UNIX socket client disconnects mid-command."""


def _set_pty_winsize(fd: int, rows: int = 24, cols: int = 32768) -> None:
    """Sets wide PTY dimensions to prevent line-wrapping of long commands."""
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


def _is_client_disconnected(abort_sock: Optional[socket.socket]) -> bool:
    """Returns True if `abort_sock` has hit EOF or closed from the client side."""
    if abort_sock is None:
        return False
    try:
        r, _, _ = select.select([abort_sock], [], [], 0.0)
        if abort_sock in r:
            peek = abort_sock.recv(1, socket.MSG_PEEK)
            if not peek:
                return True
    except OSError:
        return True
    return False


def _drain_pty(master_fd: int, duration: float = 0.05) -> bytes:
    """Non-blockingly drains available bytes from `master_fd` up to `duration` seconds."""
    drained = bytearray()
    deadline = time.monotonic() + max(0.0, duration)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 and drained:
            break
        wait_for = max(0.0, remaining)
        r, _, _ = select.select([master_fd], [], [], wait_for)
        if master_fd not in r:
            break
        while True:
            try:
                chunk = os.read(master_fd, 65536)
                if not chunk:
                    raise EOFError("Remote terminal session closed unexpectedly.")
                drained.extend(chunk)
            except BlockingIOError:
                break
        if time.monotonic() >= deadline:
            break
    return bytes(drained)


def _write_pty_nonblocking(
    master_fd: int,
    data: bytes,
    idle_timeout: float = 30.0,
    deadline: Optional[float] = None,
    on_output: Optional[Callable[[bytes], None]] = None,
    abort_sock: Optional[socket.socket] = None,
) -> bytes:
    """
    Writes `data` to `master_fd` using non-blocking I/O while simultaneously
    draining PTY output and watching `abort_sock` for client disconnect.
    Prevents kernel PTY buffer deadlocks from hanging past timeout.
    """
    try:
        if os.get_blocking(master_fd):
            os.set_blocking(master_fd, False)
    except OSError:
        pass

    drained = bytearray()
    view = memoryview(data)
    offset = 0
    chunk_size = 512
    eff_idle = max(float(idle_timeout), 0.2)
    last_progress = time.monotonic()

    while offset < len(view):
        if _is_client_disconnected(abort_sock):
            raise ClientDisconnectedError("Client disconnected while writing command input.")

        now = time.monotonic()
        if deadline is not None and now > deadline:
            raise TimeoutError("Timed out writing command input to background terminal session.")
        if now - last_progress > eff_idle:
            raise TimeoutError(
                f"Timed out writing command input after {eff_idle}s of no progress."
            )

        wait_slice = min(0.25, max(0.01, eff_idle - (now - last_progress)))
        if deadline is not None:
            wait_slice = min(wait_slice, max(0.01, deadline - now))

        rlist = [master_fd] + ([abort_sock] if abort_sock is not None else [])
        r, w, _ = select.select(rlist, [master_fd], [], wait_slice)

        if abort_sock is not None and abort_sock in r and _is_client_disconnected(abort_sock):
            raise ClientDisconnectedError("Client disconnected while writing command input.")

        if master_fd in r:
            while True:
                try:
                    out = os.read(master_fd, 65536)
                    if not out:
                        raise EOFError("Remote terminal session closed unexpectedly.")
                    last_progress = time.monotonic()
                    if on_output is not None:
                        on_output(out)
                    else:
                        drained.extend(out)
                except BlockingIOError:
                    break

        if master_fd in w and offset < len(view):
            end = min(offset + chunk_size, len(view))
            try:
                written = os.write(master_fd, view[offset:end])
                if written > 0:
                    offset += written
                    last_progress = time.monotonic()
            except BlockingIOError:
                pass

    return bytes(drained)


def _decode_b64_chunk(
    raw_slice: bytes,
    carry: bytearray,
    callback: Optional[Callable[[bytes], None]],
    final: bool = False,
) -> None:
    """Incrementally decodes base64 output in 4-char quanta, invoking `callback`."""
    if raw_slice:
        carry.extend(b"".join(raw_slice.split()))
    if not carry:
        return
    if final:
        data_to_decode = bytes(carry)
        carry.clear()
    else:
        dec_len = (len(carry) // 4) * 4
        if dec_len <= 0:
            return
        data_to_decode = bytes(carry[:dec_len])
        del carry[:dec_len]
    decoded = base64.b64decode(data_to_decode, validate=True)
    if decoded and callback is not None:
        callback(decoded)


def _probe_pty_alive(master_fd: int, timeout: float = 3.0) -> bool:
    """Verifies the remote shell inside `master_fd` executes a lightweight sentinel."""
    tok = uuid.uuid4().hex[:10]
    marker = f"__HPC_PING_{tok}__".encode("ascii")
    # Split string in shell so echo (if ever enabled) cannot match `marker` literally
    probe_cmd = f"printf '__HPC_PING_''{tok}__\\n'\n".encode("ascii")
    buf = bytearray()

    def _collect(chunk: bytes) -> None:
        buf.extend(chunk)

    try:
        _write_pty_nonblocking(
            master_fd,
            probe_cmd,
            idle_timeout=timeout,
            deadline=time.monotonic() + timeout,
            on_output=_collect,
        )
    except (TimeoutError, EOFError, OSError, ClientDisconnectedError):
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        idx = buf.find(marker)
        if idx != -1:
            if buf.find(b"\n", idx + len(marker)) != -1:
                try:
                    _drain_pty(master_fd, duration=0.02)
                except (EOFError, OSError):
                    return False
                return True
        remaining = max(0.01, min(0.2, deadline - time.monotonic()))
        r, _, _ = select.select([master_fd], [], [], remaining)
        if master_fd in r:
            while True:
                try:
                    out = os.read(master_fd, 65536)
                    if not out:
                        return False
                    buf.extend(out)
                except BlockingIOError:
                    break
    return False


def _recover_pty_after_interrupt(
    master_fd: int,
    token: str,
    timeout: float = 4.0,
) -> bool:
    """
    Interrupts an in-flight foreground command on `master_fd`, removes temporary
    files created for `token`, and verifies the shell prompt is synchronized again.
    """
    sync_tok = uuid.uuid4().hex[:10]
    sync_marker = f"__HPC_SYNC_{sync_tok}__".encode("ascii")
    try:
        try:
            _write_pty_nonblocking(master_fd, b"\x03\x03", idle_timeout=1.5, deadline=time.monotonic() + 1.5)
        except TimeoutError:
            return False
        try:
            _drain_pty(master_fd, duration=0.15)
        except EOFError:
            return False

        cleanup_cmd = (
            f"rm -rf \"${{TMPDIR:-/tmp}}/.hpc_{token}_\"* \"/tmp/.hpc_{token}_\"* 2>/dev/null; "
            f"printf '__HPC_SYNC_''{sync_tok}__\\n'\n"
        ).encode("utf-8")
        buf = bytearray()
        deadline = time.monotonic() + timeout

        def _collect(chunk: bytes) -> None:
            buf.extend(chunk)

        _write_pty_nonblocking(
            master_fd,
            cleanup_cmd,
            idle_timeout=timeout,
            deadline=deadline,
            on_output=_collect,
        )
        while time.monotonic() < deadline:
            idx = buf.find(sync_marker)
            if idx != -1 and buf.find(b"\n", idx + len(sync_marker)) != -1:
                _drain_pty(master_fd, duration=0.02)
                return True
            remaining = max(0.01, min(0.2, deadline - time.monotonic()))
            r, _, _ = select.select([master_fd], [], [], remaining)
            if master_fd in r:
                while True:
                    try:
                        out = os.read(master_fd, 65536)
                        if not out:
                            return False
                        buf.extend(out)
                    except BlockingIOError:
                        break
    except (TimeoutError, EOFError, OSError, ClientDisconnectedError):
        return False
    return False


def _safe_prefix_len(buf: bytearray, marker: bytes) -> int:
    """
    Returns the leading byte count in `buf` that cannot overlap the start of
    `marker` (assuming `marker` is not yet contained in `buf`).
    """
    max_k = min(len(buf), len(marker) - 1)
    for k in range(max_k, 0, -1):
        if marker.startswith(bytes(buf[-k:])):
            return len(buf) - k
    return len(buf)


def _send_pty_step_wait(
    master_fd: int,
    step_cmd: bytes,
    marker_prefix: bytes,
    idle_timeout: float,
    deadline: Optional[float],
    abort_sock: Optional[socket.socket] = None,
) -> Tuple[str, bytes]:
    """
    Sends a short (< 512B) flow-controlled setup step to `master_fd` and waits
    for a `<marker_prefix><status>\\n` acknowledgment line.
    Returns `(status_text, trailing_bytes_after_ack)`.
    """
    buf = bytearray()
    eff_idle = max(float(idle_timeout), 0.2)

    _write_pty_nonblocking(
        master_fd,
        step_cmd,
        idle_timeout=min(eff_idle, 30.0) if not deadline else eff_idle,
        deadline=deadline,
        on_output=buf.extend,
        abort_sock=abort_sock,
    )

    last_progress = time.monotonic()
    while True:
        idx = buf.find(marker_prefix)
        if idx != -1:
            nl_idx = buf.find(b"\n", idx + len(marker_prefix))
            if nl_idx != -1:
                status_text = bytes(buf[idx + len(marker_prefix) : nl_idx]).decode("utf-8", errors="ignore").strip()
                trailing = bytes(buf[nl_idx + 1 :])
                return status_text, trailing

        if _is_client_disconnected(abort_sock):
            raise ClientDisconnectedError("Client disconnected during command setup.")

        now = time.monotonic()
        if deadline is not None and now > deadline:
            raise TimeoutError("Timed out during command setup on background terminal session.")
        if now - last_progress > eff_idle:
            raise TimeoutError(
                f"Command setup stalled for {eff_idle}s without response on background terminal session."
            )

        wait_slice = min(0.25, max(0.01, eff_idle - (now - last_progress)))
        if deadline is not None:
            wait_slice = min(wait_slice, max(0.01, deadline - now))

        rlist = [master_fd] + ([abort_sock] if abort_sock is not None else [])
        r, _, _ = select.select(rlist, [], [], wait_slice)

        if abort_sock is not None and abort_sock in r and _is_client_disconnected(abort_sock):
            raise ClientDisconnectedError("Client disconnected during command setup.")

        if master_fd in r:
            while True:
                try:
                    out = os.read(master_fd, 65536)
                    if not out:
                        raise EOFError("Remote terminal session closed during command setup.")
                    last_progress = time.monotonic()
                    buf.extend(out)
                except BlockingIOError:
                    break


def _upload_b64_chunked(
    master_fd: int,
    token: str,
    tag: str,
    b64_text: str,
    target_rel_path: str,
    idle_timeout: float,
    deadline: Optional[float],
    abort_sock: Optional[socket.socket] = None,
) -> bytes:
    """
    Uploads `b64_text` to `"$_d/{target_rel_path}"` in 384-byte ACKed chunks so
    canonical PTY buffers (macOS 1KB / Linux 4KB) never overflow or corrupt data.
    """
    trailing = b""
    for idx, pos in enumerate(range(0, len(b64_text), 384)):
        chunk_str = b64_text[pos : pos + 384]
        marker_prefix = f"__HPC_K_{token}_{tag}_{idx}__:".encode("ascii")
        step = (
            f"printf '%s\\n' '{chunk_str}' >>\"$_d/{target_rel_path}\" "
            f"&& printf '__HPC_K_{token}_{tag}_''{idx}__:0\\n' "
            f"|| printf '__HPC_K_{token}_{tag}_''{idx}__:1\\n'\n"
        ).encode("ascii")
        status, trailing = _send_pty_step_wait(
            master_fd,
            step,
            marker_prefix,
            idle_timeout=idle_timeout,
            deadline=deadline,
            abort_sock=abort_sock,
        )
        if status != "0":
            raise RuntimeError(f"Failed staging remote {target_rel_path} chunk {idx} (status={status}).")
    return trailing


def _execute_on_pty_stream(
    master_fd: int,
    cmd: str,
    stdin_bytes: bytes = b"",
    timeout: float = 0.0,
    idle_timeout: float = 120.0,
    on_stdout: Optional[Callable[[bytes], None]] = None,
    on_stderr: Optional[Callable[[bytes], None]] = None,
    abort_sock: Optional[socket.socket] = None,
) -> int:
    """
    Executes `cmd` on the live remote PTY session with isolated stderr and lossless
    incremental streaming of stdout and stderr chunks.
    Compresses `stdin_bytes` with gzip when beneficial and uploads larger inputs in
    flow-controlled ACKed chunks so canonical PTY buffers never overflow.
    """
    try:
        if os.get_blocking(master_fd):
            os.set_blocking(master_fd, False)
    except OSError:
        pass

    token = uuid.uuid4().hex[:12]
    start_marker = f"__HPC_S_{token}__".encode("ascii")
    data_end_prefix = f"__HPC_D_{token}__:".encode("ascii")
    end_marker = f"__HPC_E_{token}__".encode("ascii")

    eff_idle = max(float(idle_timeout), 0.2) if idle_timeout and float(idle_timeout) > 0 else 120.0
    setup_idle = min(eff_idle, 20.0)
    deadline = (
        time.monotonic() + max(float(timeout), 0.2)
        if timeout and float(timeout) > 0
        else None
    )

    use_gzip = False
    b64_in = ""
    if stdin_bytes:
        payload_bytes = stdin_bytes
        if len(stdin_bytes) > 256:
            gz = gzip.compress(stdin_bytes, compresslevel=6)
            if len(gz) < len(stdin_bytes):
                payload_bytes = gz
                use_gzip = True
        b64_in = base64.b64encode(payload_bytes).decode("ascii")

    cmd_needs_staging = len(cmd) > 320
    stdin_needs_staging = len(b64_in) > 256
    needs_staging = cmd_needs_staging or stdin_needs_staging

    raw_buf = bytearray()
    b64_stdout_carry = bytearray()
    b64_stderr_carry = bytearray()
    state = "WAIT_START"
    exit_code = 1

    def feed_raw_output(chunk: bytes) -> None:
        nonlocal state, exit_code
        raw_buf.extend(chunk)
        while state != "DONE":
            if state == "WAIT_START":
                s_idx = raw_buf.find(start_marker)
                if s_idx == -1:
                    hold = len(start_marker) - 1
                    if hold > 0 and len(raw_buf) > hold:
                        del raw_buf[: len(raw_buf) - hold]
                    break
                nl_idx = raw_buf.find(b"\n", s_idx + len(start_marker))
                if nl_idx == -1:
                    if s_idx > 0:
                        del raw_buf[:s_idx]
                    break
                del raw_buf[: nl_idx + 1]
                state = "STREAM_STDOUT"
            elif state == "STREAM_STDOUT":
                d_idx = raw_buf.find(data_end_prefix)
                if d_idx == -1:
                    safe_upto = _safe_prefix_len(raw_buf, data_end_prefix)
                    if safe_upto > 0:
                        _decode_b64_chunk(
                            bytes(raw_buf[:safe_upto]),
                            b64_stdout_carry,
                            on_stdout,
                            final=False,
                        )
                        del raw_buf[:safe_upto]
                    break
                _decode_b64_chunk(
                    bytes(raw_buf[:d_idx]),
                    b64_stdout_carry,
                    on_stdout,
                    final=True,
                )
                del raw_buf[: d_idx + len(data_end_prefix)]
                state = "WAIT_RC"
            elif state == "WAIT_RC":
                nl_idx = raw_buf.find(b"\n")
                if nl_idx == -1:
                    break
                rc_bytes = bytes(raw_buf[:nl_idx]).strip()
                try:
                    exit_code = int(rc_bytes.decode("ascii"))
                except ValueError:
                    exit_code = 1
                del raw_buf[: nl_idx + 1]
                state = "STREAM_STDERR"
            elif state == "STREAM_STDERR":
                e_idx = raw_buf.find(end_marker)
                if e_idx == -1:
                    safe_upto = _safe_prefix_len(raw_buf, end_marker)
                    if safe_upto > 0:
                        _decode_b64_chunk(
                            bytes(raw_buf[:safe_upto]),
                            b64_stderr_carry,
                            on_stderr,
                            final=False,
                        )
                        del raw_buf[:safe_upto]
                    break
                nl_idx = raw_buf.find(b"\n", e_idx + len(end_marker))
                if nl_idx == -1:
                    break
                _decode_b64_chunk(
                    bytes(raw_buf[:e_idx]),
                    b64_stderr_carry,
                    on_stderr,
                    final=True,
                )
                del raw_buf[: nl_idx + 1]
                state = "DONE"

    try:
        setup_tail = b""
        script_parts: List[str] = ["{"]

        if needs_staging:
            init_marker = f"__HPC_I_{token}__:".encode("ascii")
            init_cmd = (
                f"_t='{token}'; "
                f"_d=$(mktemp -d \"${{TMPDIR:-/tmp}}/.hpc_${{_t}}_XXXXXX\") "
                f"&& printf '__HPC_I_''{token}__:0\\n' "
                f"|| printf '__HPC_I_''{token}__:1\\n'\n"
            ).encode("utf-8")
            status, setup_tail = _send_pty_step_wait(
                master_fd,
                init_cmd,
                init_marker,
                idle_timeout=setup_idle,
                deadline=deadline,
                abort_sock=abort_sock,
            )
            if status != "0":
                raise RuntimeError("Failed creating remote temporary directory on HPC session.")

            if stdin_bytes:
                setup_tail = _upload_b64_chunked(
                    master_fd,
                    token=token,
                    tag="in",
                    b64_text=b64_in,
                    target_rel_path="in.b64",
                    idle_timeout=setup_idle,
                    deadline=deadline,
                    abort_sock=abort_sock,
                )
                decode_stmt = (
                    '{ base64 -d <"$_d/in.b64" | gzip -d >"$_d/in"; } 2>>"$_d/err"; rm -f "$_d/in.b64"'
                    if use_gzip
                    else 'base64 -d <"$_d/in.b64" >"$_d/in" 2>>"$_d/err"; rm -f "$_d/in.b64"'
                )
                script_parts.append(decode_stmt)
                stdin_redir = '<"$_d/in"'
            else:
                stdin_redir = "</dev/null"

            if cmd_needs_staging:
                cmd_b64 = base64.b64encode(cmd.encode("utf-8")).decode("ascii")
                setup_tail = _upload_b64_chunked(
                    master_fd,
                    token=token,
                    tag="cmd",
                    b64_text=cmd_b64,
                    target_rel_path="cmd.b64",
                    idle_timeout=setup_idle,
                    deadline=deadline,
                    abort_sock=abort_sock,
                )
                script_parts.append('base64 -d <"$_d/cmd.b64" >"$_d/cmd.sh" 2>>"$_d/err"; rm -f "$_d/cmd.b64"')
                cmd_body = '. "$_d/cmd.sh"'
            else:
                cmd_body = cmd
        else:
            script_parts.append(f"_t='{token}'")
            script_parts.append('_d=$(mktemp -d "${TMPDIR:-/tmp}/.hpc_${_t}_XXXXXX")')
            if stdin_bytes:
                decode_pipe = 'base64 -d 2>>"$_d/err" | gzip -d >"$_d/in" 2>>"$_d/err"' if use_gzip else 'base64 -d >"$_d/in" 2>>"$_d/err"'
                script_parts.append(f"printf '%s\\n' '{b64_in}' | {decode_pipe}")
                stdin_redir = '<"$_d/in"'
            else:
                stdin_redir = "</dev/null"
            cmd_body = cmd

        script_parts.extend([
            "printf '__HPC_S_%s__\\n' \"$_t\"",
            f"{{ (\n{cmd_body}\n); echo $? >\"$_d/rc\"; }} {stdin_redir} 2>>\"$_d/err\" | base64 2>>\"$_d/err\"",
            "printf '\\n__HPC_D_%s__:%s\\n' \"$_t\" \"$(cat \"$_d/rc\" 2>/dev/null || echo 1)\"",
            'if [ -s "$_d/err" ]; then base64 <"$_d/err" 2>/dev/null; fi',
            'rm -rf "$_d"',
            "printf '\\n__HPC_E_%s__\\n' \"$_t\"",
            "}",
        ])
        full_script = ("\n".join(script_parts) + "\n").encode("utf-8")

        if setup_tail:
            feed_raw_output(setup_tail)

        _write_pty_nonblocking(
            master_fd,
            full_script,
            idle_timeout=min(eff_idle, 30.0) if not deadline else eff_idle,
            deadline=deadline,
            on_output=feed_raw_output,
            abort_sock=abort_sock,
        )

        last_progress = time.monotonic()
        while state != "DONE":
            if _is_client_disconnected(abort_sock):
                raise ClientDisconnectedError("Client disconnected while waiting for command output.")

            now = time.monotonic()
            if deadline is not None and now > deadline:
                raise TimeoutError(f"Command timed out after {timeout}s on background terminal session.")
            if now - last_progress > eff_idle:
                raise TimeoutError(
                    f"Command stalled for {eff_idle}s without output on background terminal session."
                )

            wait_slice = min(0.5, max(0.01, eff_idle - (now - last_progress)))
            if deadline is not None:
                wait_slice = min(wait_slice, max(0.01, deadline - now))

            rlist = [master_fd] + ([abort_sock] if abort_sock is not None else [])
            r, _, _ = select.select(rlist, [], [], wait_slice)

            if abort_sock is not None and abort_sock in r and _is_client_disconnected(abort_sock):
                raise ClientDisconnectedError("Client disconnected while waiting for command output.")

            if master_fd in r:
                while state != "DONE":
                    try:
                        chunk = os.read(master_fd, 65536)
                        if not chunk:
                            raise EOFError("Remote terminal session closed during command execution.")
                        last_progress = time.monotonic()
                        feed_raw_output(chunk)
                    except BlockingIOError:
                        break
    except (ClientDisconnectedError, TimeoutError, RuntimeError) as exc:
        healthy = _recover_pty_after_interrupt(master_fd, token, timeout=4.0)
        try:
            setattr(exc, "pty_healthy", healthy)
        except Exception:
            pass
        raise

    return exit_code


def _execute_on_pty(
    master_fd: int,
    cmd: str,
    stdin_bytes: bytes = b"",
    timeout: float = 120.0,
    idle_timeout: float = 120.0,
) -> Tuple[int, bytes]:
    """
    Executes `cmd` on the live remote PTY session and returns `(exit_code, stdout_bytes)`.
    """
    out_buf = bytearray()
    rc = _execute_on_pty_stream(
        master_fd,
        cmd=cmd,
        stdin_bytes=stdin_bytes,
        timeout=timeout,
        idle_timeout=idle_timeout,
        on_stdout=out_buf.extend,
    )
    return rc, bytes(out_buf)


def _write_worker_err(sock_path: str, reason: str) -> None:
    """Records worker daemon startup failure details for fast caller feedback."""
    err_path = get_worker_err_path(sock_path)
    try:
        Path(err_path).write_text(reason.strip() + "\n", encoding="utf-8")
    except OSError:
        pass


def _terminate_pid(pid: int, sig: int = signal.SIGTERM) -> None:
    """Safely sends a signal to a child or worker process and reaps if child."""
    if pid <= 0 or pid == os.getpid():
        return
    try:
        os.kill(pid, sig)
    except OSError:
        return


def _reap_child_nonblocking(pid: int) -> bool:
    """Returns True if child `pid` has exited."""
    if pid <= 0:
        return True
    try:
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        return wpid == pid
    except ChildProcessError:
        return True
    except OSError:
        return False


def _clean_ansi(raw_bytes: bytes) -> str:
    """Strips ANSI OSC/CSI escape sequences from terminal banner/prompt bytes."""
    raw_clean = raw_bytes.decode("utf-8", errors="ignore")
    clean = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", raw_clean)
    clean = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", clean)
    clean = re.sub(r"\x1b[\(\)][A-Za-z0-9]", "", clean)
    return clean


def _read_single_json_line(conn: socket.socket, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """Reads one newline-terminated JSON object from `conn` with a bounded timeout."""
    conn.settimeout(timeout)
    raw = bytearray()
    try:
        while b"\n" not in raw:
            chunk = conn.recv(65536)
            if not chunk:
                break
            raw.extend(chunk)
    finally:
        conn.settimeout(None)
    if not raw:
        return None
    line = raw.split(b"\n", 1)[0].decode("utf-8", errors="ignore").strip()
    if not line:
        return None
    return json.loads(line)


def run_worker_server(
    sock_path: str,
    master_ssh_sock: str,
    user: str,
    host: str,
    port: int = 22,
    default_node: Optional[str] = None,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    first_byte_timeout: float = 12.0,
) -> None:
    """
    Maintains a persistent background interactive PTY session to the HPC headnode.
    Listens on `sock_path` and executes commands (including binary `tar`) over
    the live terminal session while keeping it alive permanently.
    """
    pid_path = get_worker_pid_path(sock_path)
    err_path = get_worker_err_path(sock_path)
    try:
        Path(pid_path).write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    if os.path.exists(err_path):
        try:
            os.unlink(err_path)
        except OSError:
            pass

    master, slave = pty.openpty()
    _set_pty_winsize(master, rows=24, cols=32768)
    _set_pty_winsize(slave, rows=24, cols=32768)

    pid = os.fork()
    if pid == 0:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.close(master)
        os.setsid()
        os.dup2(slave, 0)
        os.dup2(slave, 1)
        os.dup2(slave, 2)
        if slave > 2:
            os.close(slave)
        ssh_cmd = [
            "ssh",
            "-tt",
            "-o", "BatchMode=yes",
            "-o", "ControlMaster=no",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=6",
            "-S", str(master_ssh_sock),
        ]
        if port and int(port) != 22:
            ssh_cmd.extend(["-p", str(port)])
        ssh_cmd.append(f"{user}@{host}")
        os.execlp("ssh", *ssh_cmd)
    os.close(slave)

    def _on_sigterm(_signum, _frame) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)

    try:
        os.set_blocking(master, False)
    except OSError:
        pass

    # Intercept bastion countdown (if any) and wait for headnode shell prompt
    startup_idle_timeout = max(0.2, float(first_byte_timeout))
    if startup_idle_timeout < 5.0:
        total_startup_budget = max(2.5, startup_idle_timeout * 3.0)
    else:
        total_startup_budget = min(32.0, max(20.0, startup_idle_timeout * 2.5))

    buf = b""
    hist_buf = bytearray()
    got_any_output = False
    t0 = time.monotonic()
    last_activity = t0
    sent_c = False
    sent_node = False
    ready = False
    shell_verified = False
    is_bastion = is_bastion_host(host)
    deadline = t0 + total_startup_budget
    startup_err = "Timed out waiting for remote shell prompt."
    auth_prompt_re = re.compile(
        r"(?:^|[\r\n])[^\r\n]*(?:password|passcode|verification code|enter pin)\s*:\s*$"
        r"|duo two-factor|permission denied \(",
        re.IGNORECASE,
    )

    def _record_hist(data: bytes) -> None:
        if not data:
            return
        hist_buf.extend(data)
        if len(hist_buf) > 4096:
            del hist_buf[: len(hist_buf) - 2048]

    def _startup_stage() -> str:
        if not got_any_output:
            return "before any remote response"
        if is_bastion and sent_node:
            target = (default_node or "").strip() or "headnode"
            return f"connecting to '{target}'"
        if is_bastion and sent_c:
            return "at bastion hostname prompt"
        if is_bastion:
            return "at bastion banner"
        return "waiting for shell prompt"

    def _cleanup_startup_ssh() -> None:
        if not _reap_child_nonblocking(pid):
            _terminate_pid(pid, signal.SIGTERM)
            wait_end = time.monotonic() + 0.25
            while time.monotonic() < wait_end and not _reap_child_nonblocking(pid):
                time.sleep(0.02)
            if not _reap_child_nonblocking(pid):
                _terminate_pid(pid, signal.SIGKILL)
                _reap_child_nonblocking(pid)
        try:
            os.close(master)
        except OSError:
            pass
        if os.path.exists(pid_path):
            try:
                os.unlink(pid_path)
            except OSError:
                pass

    srv: Optional[socket.socket] = None
    try:
        while time.monotonic() < deadline:
            if _reap_child_nonblocking(pid):
                startup_err = _clean_ansi(bytes(hist_buf[-300:])).strip() or "SSH child exited before shell became ready."
                break
            now = time.monotonic()
            if (now - last_activity) > startup_idle_timeout:
                snippet = " ".join(_clean_ansi(bytes(hist_buf[-200:])).split())
                if not got_any_output:
                    startup_err = (
                        f"Remote SSH session did not respond within {startup_idle_timeout:.0f}s "
                        "(stale ControlMaster or unreachable host)."
                    )
                else:
                    detail = f": {snippet}" if snippet else "."
                    startup_err = (
                        f"Remote SSH session stalled for {startup_idle_timeout:.0f}s {_startup_stage()} "
                        f"(stale ControlMaster or unresponsive host){detail}"
                    )
                break

            remaining = max(
                0.02,
                min(0.25, deadline - now, startup_idle_timeout - (now - last_activity)),
            )
            r, _, _ = select.select([master], [], [], remaining)
            if master in r:
                try:
                    got_eof = False
                    while True:
                        try:
                            chunk = os.read(master, 4096)
                            if not chunk:
                                got_eof = True
                                break
                            got_any_output = True
                            last_activity = time.monotonic()
                            buf += chunk
                            _record_hist(chunk)
                        except BlockingIOError:
                            break
                    if got_eof:
                        startup_err = _clean_ansi(bytes(hist_buf[-300:])).strip() or "Remote SSH terminal closed during startup."
                        break

                    clean_recent = _clean_ansi(bytes(hist_buf[-500:])).replace("\r\n", "\n").replace("\r", "\n")
                    if auth_prompt_re.search(clean_recent):
                        snippet = " ".join(clean_recent[-200:].split())
                        startup_err = (
                            f"Remote SSH session requested authentication or denied access "
                            f"({snippet}) — stale or expired ControlMaster."
                        )
                        break

                    low_buf = buf.lower()
                    if is_bastion:
                        if not sent_c and (b"hit ^c" in low_buf or b"within 5 seconds" in low_buf):
                            time.sleep(0.02)
                            drained = b""
                            try:
                                drained = _write_pty_nonblocking(master, b"\x03", idle_timeout=2.0)
                            except (TimeoutError, EOFError, OSError, ClientDisconnectedError):
                                pass
                            _record_hist(drained)
                            sent_c = True
                            last_activity = time.monotonic()
                            buf = drained
                            low_buf = buf.lower()

                        if sent_c and not sent_node and (
                            b"enter a hostname" in low_buf
                            or b"hostname [" in low_buf
                            or b"enter a host" in low_buf
                        ):
                            node_str = (default_node or "").strip()
                            time.sleep(0.02)
                            drained = b""
                            try:
                                drained = _write_pty_nonblocking(
                                    master,
                                    f"{node_str}\n".encode("utf-8") if node_str else b"\n",
                                    idle_timeout=2.0,
                                )
                            except (TimeoutError, EOFError, OSError, ClientDisconnectedError):
                                pass
                            _record_hist(drained)
                            sent_node = True
                            last_activity = time.monotonic()
                            buf = drained
                except OSError as exc:
                    startup_err = f"Terminal read error during startup: {exc}"
                    break

            if is_bastion:
                ready_to_check = sent_node or (
                    not sent_c and (time.monotonic() - t0) > min(4.0, startup_idle_timeout * 0.6)
                )
            else:
                ready_to_check = True

            if ready_to_check and buf and not ready:
                clean = _clean_ansi(buf)
                if PROMPT_REGEX.search(clean.strip()):
                    ready = True
                    break

        if not ready and startup_err == "Timed out waiting for remote shell prompt.":
            snippet = " ".join(_clean_ansi(bytes(hist_buf[-200:])).split())
            if snippet:
                startup_err = f"Timed out waiting for remote shell prompt ({_startup_stage()}): {snippet}"

        # Verify interactive POSIX shell execution and configure echo/prompt silence
        if ready:
            startup_err = ""
            probe_tok = uuid.uuid4().hex[:10]
            probe_marker = f"__HPC_READY_{probe_tok}__".encode("ascii")
            init_cmd = (
                "stty -echo 2>/dev/null || true; "
                "PS1=''; PS2=''; unset PROMPT_COMMAND 2>/dev/null || true; "
                "set +o verbose +o xtrace 2>/dev/null || true; "
                f"printf '__HPC_READY_''{probe_tok}__\\n'\n"
            ).encode("utf-8")
            verify_buf = bytearray()
            verify_window = min(8.0, max(2.5, startup_idle_timeout * 2.0))
            verify_deadline = min(t0 + total_startup_budget + verify_window, time.monotonic() + verify_window)
            try:
                _write_pty_nonblocking(
                    master,
                    init_cmd,
                    idle_timeout=min(5.0, verify_window),
                    deadline=verify_deadline,
                    on_output=verify_buf.extend,
                )
                while time.monotonic() < verify_deadline:
                    idx = verify_buf.find(probe_marker)
                    if idx != -1 and verify_buf.find(b"\n", idx + len(probe_marker)) != -1:
                        _drain_pty(master, duration=0.05)
                        shell_verified = True
                        break
                    rem = max(0.02, min(0.2, verify_deadline - time.monotonic()))
                    r, _, _ = select.select([master], [], [], rem)
                    if master in r:
                        while True:
                            try:
                                c = os.read(master, 4096)
                                if not c:
                                    raise EOFError("Remote terminal closed during shell verification.")
                                verify_buf.extend(c)
                            except BlockingIOError:
                                break
            except (TimeoutError, EOFError, OSError, ClientDisconnectedError) as exc:
                startup_err = f"Shell initialization failed: {exc}"
            if not shell_verified and not startup_err:
                snippet = " ".join(_clean_ansi(bytes(verify_buf[-200:])).split())
                startup_err = (
                    f"Remote shell did not acknowledge initialization probe: {snippet}"
                    if snippet
                    else "Remote shell did not acknowledge initialization probe."
                )

        if not (ready and shell_verified):
            _write_worker_err(sock_path, startup_err)
            _cleanup_startup_ssh()
            return

        if os.path.exists(sock_path):
            try:
                os.unlink(sock_path)
            except OSError:
                pass

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(sock_path)
            os.chmod(sock_path, 0o600)
            srv.listen(8)
        except Exception as exc:
            _write_worker_err(sock_path, f"Failed binding worker socket {sock_path}: {exc}")
            try:
                srv.close()
            except OSError:
                pass
            _cleanup_startup_ssh()
            return
    except BaseException:
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        _cleanup_startup_ssh()
        raise

    last_active = time.monotonic()
    last_keepalive = time.monotonic()
    ssh_pid_alive = True

    def _send_json_line(conn: socket.socket, obj: Dict[str, Any]) -> None:
        raw_line = (json.dumps(obj) + "\n").encode("utf-8")
        try:
            conn.sendall(raw_line)
        except OSError as exc:
            raise ClientDisconnectedError(f"Client socket write failed: {exc}") from exc

    try:
        while True:
            if _reap_child_nonblocking(pid):
                ssh_pid_alive = False
                break

            r, _, _ = select.select([srv, master], [], [], 5.0)
            if master in r:
                got_eof = False
                try:
                    while True:
                        try:
                            c = os.read(master, 4096)
                            if not c:
                                got_eof = True
                                break
                        except BlockingIOError:
                            break
                except OSError:
                    got_eof = True
                if got_eof:
                    break

            if srv in r:
                conn, _ = srv.accept()
                last_active = time.monotonic()
                should_exit_server = False
                stream_mode = False
                try:
                    req = _read_single_json_line(conn, timeout=5.0)
                    if not req:
                        continue

                    cmd_to_run = req.get("cmd", "")
                    if cmd_to_run == "exit":
                        try:
                            _send_json_line(conn, {"code": 0, "stdout_b64": ""})
                        except ClientDisconnectedError:
                            pass
                        break

                    if cmd_to_run == "ping" or not cmd_to_run:
                        if _reap_child_nonblocking(pid):
                            ssh_pid_alive = False
                            try:
                                _send_json_line(conn, {"code": 1, "stdout_b64": "", "error": "SSH child exited."})
                            except ClientDisconnectedError:
                                pass
                            break
                        # Check for unexpected EOF on master before replying healthy
                        mr, _, _ = select.select([master], [], [], 0.0)
                        if master in mr:
                            try:
                                if not os.read(master, 4096):
                                    try:
                                        _send_json_line(conn, {"code": 1, "stdout_b64": "", "error": "Remote terminal closed."})
                                    except ClientDisconnectedError:
                                        pass
                                    break
                            except BlockingIOError:
                                pass
                            except OSError:
                                break
                        if req.get("probe_remote"):
                            ok = _probe_pty_alive(master, timeout=3.0)
                            if not ok:
                                try:
                                    _send_json_line(
                                        conn,
                                        {"code": 1, "stdout_b64": "", "error": "Remote shell probe failed."},
                                    )
                                except ClientDisconnectedError:
                                    pass
                                break
                        try:
                            _send_json_line(conn, {"code": 0, "stdout_b64": ""})
                        except ClientDisconnectedError:
                            pass
                        continue

                    stdin_b64 = req.get("stdin_b64", "")
                    stdin_bytes = base64.b64decode(stdin_b64) if stdin_b64 else b""
                    cmd_timeout = float(req.get("timeout", 0.0) or 0.0)
                    cmd_idle_timeout = float(req.get("idle_timeout", 120.0) or 120.0)
                    stream_mode = bool(req.get("stream", False))

                    if stream_mode:
                        def _emit_stdout(chunk: bytes) -> None:
                            _send_json_line(
                                conn,
                                {"type": "stdout", "b64": base64.b64encode(chunk).decode("ascii")},
                            )

                        def _emit_stderr(chunk: bytes) -> None:
                            _send_json_line(
                                conn,
                                {"type": "stderr", "b64": base64.b64encode(chunk).decode("ascii")},
                            )

                        rc = _execute_on_pty_stream(
                            master,
                            cmd=cmd_to_run,
                            stdin_bytes=stdin_bytes,
                            timeout=cmd_timeout,
                            idle_timeout=cmd_idle_timeout,
                            on_stdout=_emit_stdout,
                            on_stderr=_emit_stderr,
                            abort_sock=conn,
                        )
                        try:
                            _send_json_line(conn, {"type": "done", "code": rc, "error": ""})
                        except ClientDisconnectedError:
                            pass
                    else:
                        out_buf = bytearray()
                        err_buf = bytearray()
                        rc = _execute_on_pty_stream(
                            master,
                            cmd=cmd_to_run,
                            stdin_bytes=stdin_bytes,
                            timeout=cmd_timeout if cmd_timeout > 0 else 120.0,
                            idle_timeout=cmd_idle_timeout,
                            on_stdout=out_buf.extend,
                            on_stderr=err_buf.extend,
                            abort_sock=conn,
                        )
                        resp: Dict[str, Any] = {
                            "code": rc,
                            "stdout_b64": base64.b64encode(bytes(out_buf)).decode("ascii"),
                        }
                        if err_buf:
                            resp["stderr_b64"] = base64.b64encode(bytes(err_buf)).decode("ascii")
                        try:
                            _send_json_line(conn, resp)
                        except ClientDisconnectedError:
                            pass
                except ClientDisconnectedError as exc:
                    if not getattr(exc, "pty_healthy", True):
                        should_exit_server = True
                except Exception as exc:
                    err_obj = (
                        {"type": "done", "code": 1, "error": str(exc)}
                        if stream_mode
                        else {"code": 1, "stdout_b64": "", "error": str(exc)}
                    )
                    try:
                        _send_json_line(conn, err_obj)
                    except ClientDisconnectedError:
                        pass
                    if isinstance(exc, EOFError) or not getattr(exc, "pty_healthy", True):
                        should_exit_server = True
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass
                if should_exit_server:
                    break

            now = time.monotonic()
            # Keep remote shell $TMOUT alive with a periodic no-op comment every 60s
            if now - last_keepalive >= 60.0:
                try:
                    _write_pty_nonblocking(master, b"# keepalive\n", idle_timeout=5.0)
                    last_keepalive = now
                except (TimeoutError, EOFError, OSError, ClientDisconnectedError):
                    break

            if idle_timeout > 0 and (now - last_active > idle_timeout):
                break
    finally:
        try:
            _write_pty_nonblocking(master, b"exit\n", idle_timeout=1.0)
        except Exception:
            pass
        try:
            os.close(master)
        except OSError:
            pass
        if ssh_pid_alive and not _reap_child_nonblocking(pid):
            _terminate_pid(pid, signal.SIGTERM)
            time.sleep(0.05)
            if not _reap_child_nonblocking(pid):
                _terminate_pid(pid, signal.SIGKILL)
                _reap_child_nonblocking(pid)
        for path in (sock_path, pid_path):
            if os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        try:
            srv.close()
        except OSError:
            pass


def spawn_worker(
    sock_path: str,
    master_ssh_sock: str,
    user: str,
    host: str,
    port: int = 22,
    default_node: Optional[str] = None,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    first_byte_timeout: float = 12.0,
) -> None:
    """Spawns the persistent background terminal worker daemon."""
    for path in (sock_path, get_worker_pid_path(sock_path), get_worker_err_path(sock_path)):
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass

    python_exe = str(venv_python) if venv_python.is_file() else sys.executable
    script_path = str(Path(__file__).resolve())
    cmd = [
        python_exe,
        script_path,
        "--worker-daemon",
        "--sock-path", sock_path,
        "--master-sock", master_ssh_sock,
        "--worker-user", user,
        "--worker-host", host,
        "--worker-port", str(port),
        "--idle-timeout", str(idle_timeout),
        "--first-byte-timeout", str(first_byte_timeout),
    ]
    if default_node:
        cmd.extend(["--default-node", default_node])

    detacher = (
        "import subprocess, sys; "
        "subprocess.Popen("
        "sys.argv[1:], "
        "stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL, "
        "start_new_session=True, "
        "close_fds=True"
        ")"
    )
    subprocess.run(
        [python_exe, "-c", detacher, *cmd],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _is_pid_running(pid: int) -> bool:
    """Returns True if process `pid` currently exists."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def ensure_background_terminal_session(
    username: str,
    hpc_address: str,
    hpc: str,
    master_sock: Optional[Path] = None,
    default_node: Optional[str] = None,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    first_byte_timeout: float = 12.0,
    show_spinner: bool = False,
) -> bool:
    """
    Ensures the persistent background terminal session is running for `hpc`.
    Reuses an existing session only if it responds to a live remote ping.
    Automatically stops and replaces any wedged/unresponsive worker daemon.
    """
    worker_sock = get_worker_sock_path(username, hpc)
    if os.path.exists(worker_sock):
        if ping_worker_session(worker_sock, timeout=3.0, probe_remote=True):
            return True
        stop_background_terminal_session(username, hpc, try_graceful=False)
    elif _find_worker_pids(worker_sock):
        stop_background_terminal_session(username, hpc, try_graceful=False)

    node = default_node or DEFAULT_HEAD_NODES.get(hpc.lower())
    m_sock = str(master_sock or get_master_sock_path(username, hpc_address, 22))

    spawn_worker(
        sock_path=worker_sock,
        master_ssh_sock=m_sock,
        user=username,
        host=hpc_address,
        default_node=node,
        idle_timeout=idle_timeout,
        first_byte_timeout=first_byte_timeout,
    )

    pid_path = get_worker_pid_path(worker_sock)
    err_path = get_worker_err_path(worker_sock)
    t_start = time.monotonic()
    wait_sec = 42.0
    deadline = t_start + wait_sec
    frame = 0
    bar_width = 0

    def _clear_spinner() -> None:
        nonlocal bar_width
        if show_spinner and bar_width:
            sys.stdout.write("\r" + (" " * bar_width) + "\r")
            sys.stdout.flush()
            bar_width = 0

    def _draw_spinner() -> None:
        nonlocal frame, bar_width
        if not show_spinner:
            return
        elapsed = max(0, int(time.monotonic() - t_start))
        track = 16
        slider = 4
        span = track - slider
        pos = frame % (2 * span)
        if pos > span:
            pos = (2 * span) - pos
        bar = ("." * pos) + ("=" * slider) + ("." * (span - pos))
        spinner = "|/-\\"[frame % 4]
        text = f"{spinner} [{bar}] {elapsed}s elapsed | Starting '{hpc}' background terminal session..."
        columns = max(20, shutil.get_terminal_size(fallback=(100, 24)).columns - 1)
        text = text[:columns]
        _clear_spinner()
        sys.stdout.write("\r" + text)
        sys.stdout.flush()
        bar_width = len(text)
        frame += 1

    try:
        while time.monotonic() < deadline:
            if is_socket_alive(worker_sock) and ping_worker_session(worker_sock, timeout=2.0, probe_remote=False):
                _clear_spinner()
                return True

            if os.path.exists(err_path):
                _clear_spinner()
                try:
                    reason = Path(err_path).read_text(encoding="utf-8", errors="ignore").strip()
                except OSError:
                    reason = ""
                stop_background_terminal_session(username, hpc, try_graceful=False)
                print(
                    f"\n[!] Error: Background terminal session for '{hpc}' failed to start"
                    + (f": {reason}" if reason else "."),
                    file=sys.stderr,
                )
                return False

            if time.monotonic() - t_start > 1.0 and os.path.exists(pid_path):
                try:
                    wpid = int(Path(pid_path).read_text(encoding="utf-8").strip())
                    if not _is_pid_running(wpid) and not os.path.exists(worker_sock):
                        _clear_spinner()
                        stop_background_terminal_session(username, hpc, try_graceful=False)
                        print(
                            f"\n[!] Error: Background terminal worker for '{hpc}' exited unexpectedly.",
                            file=sys.stderr,
                        )
                        return False
                except (OSError, ValueError):
                    pass

            _draw_spinner()
            time.sleep(0.15)
    except BaseException:
        _clear_spinner()
        stop_background_terminal_session(username, hpc, try_graceful=False)
        raise

    _clear_spinner()
    stop_background_terminal_session(username, hpc, try_graceful=False)
    print(
        f"\n[!] Error: Background terminal session for '{hpc}' did not become ready within {wait_sec:.0f}s.",
        file=sys.stderr,
    )
    return False


def run_remote_command(
    username: Optional[str],
    hpc: str,
    cmd: str,
    stdin_bytes: bytes = b"",
    timeout: float = 0.0,
    idle_timeout: float = 120.0,
    on_stdout: Optional[Callable[[bytes], None]] = None,
    on_stderr: Optional[Callable[[bytes], None]] = None,
) -> Tuple[int, bytes, str]:
    """
    Executes `cmd` (with optional binary `stdin_bytes`) through the persistent
    background terminal session for `hpc`, consuming streamed chunks as they arrive.
    If `username` is omitted/empty, automatically discovers the active worker
    session for `hpc`.
    `timeout <= 0` allows unlimited total duration as long as data flows within
    `idle_timeout` seconds between chunks.
    Returns (exit_code, stdout_bytes, error_message).
    """
    pref_user = _clean_config_username(username) or load_hpc_config().get(hpc.lower())
    active = find_active_worker_session(
        hpc,
        preferred_username=pref_user,
        verify_ping=True,
        probe_remote=False,
    )
    if not active:
        who = f"{username}@{hpc}" if username else hpc
        return 1, b"", f"Background terminal session is not running for {who}. Run: ush/ssh_helper.py {hpc}"

    resolved_user, worker_sock = active
    eff_total = float(timeout) if timeout and float(timeout) > 0 else 0.0
    eff_idle = float(idle_timeout) if idle_timeout and float(idle_timeout) > 0 else 120.0
    payload = {
        "cmd": cmd,
        "stdin_b64": base64.b64encode(stdin_bytes).decode("ascii") if stdin_bytes else "",
        "timeout": eff_total,
        "idle_timeout": eff_idle,
        "stream": True,
    }
    raw_req = json.dumps(payload).encode("utf-8") + b"\n"

    out_buf = bytearray()
    err_buf = bytearray()
    total_deadline = (time.monotonic() + eff_total + 10.0) if eff_total > 0 else None

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            sock_timeout = eff_idle + 10.0
            if total_deadline is not None:
                sock_timeout = min(sock_timeout, max(1.0, total_deadline - time.monotonic()))
            s.settimeout(sock_timeout)
            s.connect(worker_sock)
            s.sendall(raw_req)

            line_buf = bytearray()
            while True:
                if total_deadline is not None:
                    rem = total_deadline - time.monotonic()
                    if rem <= 0:
                        return 1, bytes(out_buf), f"Command exceeded total timeout of {eff_total}s."
                    s.settimeout(min(eff_idle + 10.0, rem))

                while b"\n" not in line_buf:
                    chunk = s.recv(65536)
                    if not chunk:
                        if line_buf.strip():
                            break
                        return 1, bytes(out_buf), "Connection closed before command completed on background terminal worker."
                    line_buf.extend(chunk)

                while b"\n" in line_buf or (line_buf.strip() and b"\n" not in line_buf):
                    if b"\n" in line_buf:
                        nl_idx = line_buf.find(b"\n")
                        raw_line = bytes(line_buf[:nl_idx])
                        del line_buf[: nl_idx + 1]
                    else:
                        raw_line = bytes(line_buf)
                        line_buf.clear()

                    text_line = raw_line.decode("utf-8", errors="ignore").strip()
                    if not text_line:
                        continue
                    try:
                        msg = json.loads(text_line)
                    except json.JSONDecodeError as exc:
                        return 1, bytes(out_buf), f"Malformed response from background terminal worker: {exc}"

                    msg_type = msg.get("type")
                    if msg_type == "stdout":
                        out_b64 = msg.get("b64", "")
                        if out_b64:
                            decoded = base64.b64decode(out_b64)
                            if on_stdout is not None:
                                on_stdout(decoded)
                            else:
                                out_buf.extend(decoded)
                    elif msg_type == "stderr":
                        err_b64 = msg.get("b64", "")
                        if err_b64:
                            decoded = base64.b64decode(err_b64)
                            if on_stderr is not None:
                                on_stderr(decoded)
                            else:
                                err_buf.extend(decoded)
                    elif msg_type == "done":
                        rc = int(msg.get("code", 1))
                        err_msg = str(msg.get("error", ""))
                        if not err_msg and err_buf and rc != 0:
                            err_msg = err_buf.decode("utf-8", errors="ignore").strip()
                        return rc, bytes(out_buf), err_msg
                    else:
                        # Legacy single-JSON response fallback
                        rc = int(msg.get("code", 1))
                        out_b64 = msg.get("stdout_b64", "")
                        if out_b64:
                            decoded = base64.b64decode(out_b64)
                            if on_stdout is not None:
                                on_stdout(decoded)
                            else:
                                out_buf.extend(decoded)
                        err_b64 = msg.get("stderr_b64", "")
                        if err_b64:
                            decoded_err = base64.b64decode(err_b64)
                            if on_stderr is not None:
                                on_stderr(decoded_err)
                            else:
                                err_buf.extend(decoded_err)
                        err_msg = str(msg.get("error", ""))
                        if not err_msg and err_buf and rc != 0:
                            err_msg = err_buf.decode("utf-8", errors="ignore").strip()
                        return rc, bytes(out_buf), err_msg
    except OSError as exc:
        return 1, bytes(out_buf), f"Failed communicating with background terminal worker ({resolved_user}): {exc}"


def _find_worker_pids(sock_path: str) -> List[int]:
    """Finds running PIDs for a worker daemon bound to `sock_path` (sidecar + ps fallback)."""
    pids: List[int] = []
    seen: set[int] = set()
    pid_path = get_worker_pid_path(sock_path)
    if os.path.exists(pid_path):
        try:
            wpid = int(Path(pid_path).read_text(encoding="utf-8").strip())
            if wpid > 0 and wpid != os.getpid():
                seen.add(wpid)
                pids.append(wpid)
        except (OSError, ValueError):
            pass

    try:
        res = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (subprocess.SubprocessError, OSError):
        return pids

    if res.returncode != 0:
        return pids

    for raw_line in res.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid <= 0 or pid == os.getpid() or pid in seen:
            continue

        tokens = parts[1].split()
        if "--worker-daemon" not in tokens:
            continue
        has_sock = f"--sock-path={sock_path}" in tokens
        if not has_sock and "--sock-path" in tokens:
            for idx, tok in enumerate(tokens[:-1]):
                if tok == "--sock-path" and tokens[idx + 1] == sock_path:
                    has_sock = True
                    break
        if has_sock:
            seen.add(pid)
            pids.append(pid)

    return pids


def stop_background_terminal_session(
    username: str,
    hpc: str,
    try_graceful: bool = True,
) -> bool:
    """Cleanly stops (or forcibly terminates if wedged) the worker session for `(username, hpc)`."""
    worker_sock = get_worker_sock_path(username, hpc)
    pid_path = get_worker_pid_path(worker_sock)
    err_path = get_worker_err_path(worker_sock)
    wpids = _find_worker_pids(worker_sock)

    existed = any(os.path.exists(p) for p in (worker_sock, pid_path, err_path)) or bool(wpids)
    if not existed:
        return False

    if try_graceful and is_socket_alive(worker_sock) and ping_worker_session(worker_sock, timeout=0.6, probe_remote=False):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.8)
                s.connect(worker_sock)
                s.sendall(b'{"cmd": "exit"}\n')
                s.recv(1024)
        except OSError:
            pass

    for wpid in wpids:
        if _is_pid_running(wpid):
            _terminate_pid(wpid, signal.SIGTERM)
            wait_until = time.monotonic() + 0.8
            while time.monotonic() < wait_until and _is_pid_running(wpid):
                time.sleep(0.05)
            if _is_pid_running(wpid):
                _terminate_pid(wpid, signal.SIGKILL)

    for path in (worker_sock, pid_path, err_path):
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass
    return True


# ==============================================================================
# CLI Entrypoint
# ==============================================================================


def print_usage(file=sys.stderr) -> None:
    print("Usage: ssh_helper.py [user_name] <gaeac6|gaeac7|ursa|hera> [options]", file=file)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ssh_helper.py",
        usage="ssh_helper.py [user_name] <gaeac6|gaeac7|ursa|hera> [options]",
        description=(
            "Multiplex SSH & Persistent Background Terminal Helper for AutoMATS.\n"
            "Reuses an active multiplex SSH session if present, or prompts for\n"
            "username and YubiKey/Duo Push authentication to establish one."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "arg1",
        nargs="?",
        help="[user_name] or target cluster (gaeac6, gaeac7, ursa, hera)",
    )
    parser.add_argument(
        "arg2",
        nargs="?",
        help="Target cluster if arg1 is user_name (gaeac6, gaeac7, ursa, hera)",
    )
    parser.add_argument(
        "-u", "--user",
        dest="opt_user",
        help="RDHPCS username override",
    )
    parser.add_argument(
        "-c", "--hpc",
        dest="opt_hpc",
        choices=list(HPC_ENDPOINTS.keys()),
        help="Target HPC cluster",
    )
    parser.add_argument(
        "--cmd",
        default=None,
        help="Execute a command on the persistent background terminal session (supports stdin piping)",
    )
    parser.add_argument(
        "--cmd-timeout",
        type=float,
        default=0.0,
        help="Optional hard total timeout in seconds for --cmd (default: 0 = unlimited while active; 120s stall limit)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Check status of multiplex SSH session and background terminal session",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Stop the background terminal session for the target cluster",
    )
    parser.add_argument(
        "--idle-timeout",
        type=int,
        default=DEFAULT_IDLE_TIMEOUT,
        help="Inactivity timeout in seconds before background session exits (default: 0 = permanent)",
    )
    parser.add_argument(
        "--config-file",
        default=None,
        help="Path to .hpc_config file (default: .hpc_config in CWD or repo root)",
    )

    # Internal background worker arguments
    parser.add_argument("--worker-daemon", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--sock-path", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--master-sock", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--worker-user", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--worker-host", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--worker-port", type=int, default=22, help=argparse.SUPPRESS)
    parser.add_argument("--default-node", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--first-byte-timeout", type=float, default=12.0, help=argparse.SUPPRESS)

    return parser.parse_args(argv)


def _execute_cli_cmd(username: Optional[str], hpc: str, cmd: str, cmd_timeout: float) -> int:
    """Runs `--cmd` via the worker session while streaming stdout and stderr live."""
    stdin_bytes = b""
    if not sys.stdin.isatty():
        try:
            stdin_bytes = sys.stdin.buffer.read()
        except Exception:
            stdin_bytes = b""

    def _write_stdout(chunk: bytes) -> None:
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()

    def _write_stderr(chunk: bytes) -> None:
        sys.stderr.buffer.write(chunk)
        sys.stderr.buffer.flush()

    rc, _, err_msg = run_remote_command(
        username=username,
        hpc=hpc,
        cmd=cmd,
        stdin_bytes=stdin_bytes,
        timeout=cmd_timeout,
        idle_timeout=120.0,
        on_stdout=_write_stdout,
        on_stderr=_write_stderr,
    )
    if rc != 0 and err_msg:
        print(f"[!] {err_msg}", file=sys.stderr)
    return rc


def _cleanup_unresponsive_workers(
    hpc: str,
    preferred_username: Optional[str] = None,
    exact_user: bool = False,
    probe_first: bool = True,
) -> None:
    """Stops and removes stale/wedged worker files and processes for `hpc`."""
    hpc_key = hpc.lower()
    suffix = f"_{hpc_key}.sock"
    users: List[str] = []
    seen_users: set[str] = set()

    if preferred_username and preferred_username != "<your_username>":
        users.append(preferred_username)
        seen_users.add(preferred_username)

    if not (exact_user and preferred_username and preferred_username != "<your_username>"):
        for cand in sorted(glob.glob(f"/tmp/hpc_term_*_{hpc_key}.sock*")):
            base = os.path.basename(cand)
            u = ""
            for ext in (".pid", ".err", ""):
                if ext and base.endswith(suffix + ext):
                    u = base[len("hpc_term_") : -len(suffix + ext)]
                    break
                if not ext and base.endswith(suffix):
                    u = base[len("hpc_term_") : -len(suffix)]
                    break
            if u and u not in seen_users:
                seen_users.add(u)
                users.append(u)

    for u in users:
        wsock = get_worker_sock_path(u, hpc_key)
        if probe_first and os.path.exists(wsock) and ping_worker_session(wsock, timeout=1.5, probe_remote=True):
            continue
        stop_background_terminal_session(u, hpc_key, try_graceful=False)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    if args.worker_daemon:
        run_worker_server(
            sock_path=args.sock_path,
            master_ssh_sock=args.master_sock,
            user=args.worker_user,
            host=args.worker_host,
            port=args.worker_port,
            default_node=args.default_node,
            idle_timeout=args.idle_timeout,
            first_byte_timeout=args.first_byte_timeout,
        )
        return 0

    def _on_cli_sigterm(_signum, _frame) -> None:
        raise SystemExit(143)

    try:
        signal.signal(signal.SIGTERM, _on_cli_sigterm)
    except ValueError:
        pass

    valid_clusters = list(HPC_ENDPOINTS.keys())
    cli_username = args.opt_user
    hpc = args.opt_hpc.lower() if args.opt_hpc else None

    pos_args = [a for a in (args.arg1, args.arg2) if a]
    if len(pos_args) == 1:
        v1 = pos_args[0].lower()
        if v1 in valid_clusters:
            hpc = v1
        else:
            cli_username = pos_args[0]
    elif len(pos_args) >= 2:
        v1 = pos_args[0].lower()
        v2 = pos_args[1].lower()
        if v2 in valid_clusters:
            cli_username = pos_args[0]
            hpc = v2
        elif v1 in valid_clusters:
            hpc = v1
            cli_username = pos_args[1]
        else:
            print(f"[!] Error: Invalid HPC cluster. Must be one of: {', '.join(valid_clusters)}", file=sys.stderr)
            print_usage()
            return 1

    if not hpc or hpc not in valid_clusters:
        print_usage()
        return 1

    clean_cli_user = _clean_config_username(cli_username)
    exact_user = bool(clean_cli_user)
    config_path = Path(args.config_file) if args.config_file else get_default_config_path()
    hpc_config = load_hpc_config(config_path)
    configured_username = hpc_config.get(hpc)
    preferred_username = clean_cli_user or configured_username
    status_out = sys.stderr if args.cmd else sys.stdout

    # Handle --status
    if args.status:
        active_master = find_active_control_master(
            hpc,
            preferred_username=preferred_username,
            exact_user=exact_user,
        )
        active_worker = find_active_worker_session(
            hpc,
            preferred_username=preferred_username,
            exact_user=exact_user,
            verify_ping=True,
            probe_remote=True,
        )
        stale_worker = None
        if not active_worker:
            stale_worker = find_active_worker_session(
                hpc,
                preferred_username=preferred_username,
                exact_user=exact_user,
                verify_ping=False,
            )
        resolved_user = (
            (active_worker[0] if active_worker else None)
            or (stale_worker[0] if stale_worker else None)
            or (active_master[0] if active_master else None)
            or preferred_username
            or "<unknown>"
        )
        print(f"Cluster:            {hpc}")
        print(f"Username:           {resolved_user}")
        print(f"Multiplex Session:  {'ACTIVE (' + active_master[1] + ')' if active_master else 'INACTIVE'}")
        if active_worker:
            bg_status = f"ACTIVE ({active_worker[1]})"
        elif stale_worker:
            bg_status = f"UNRESPONSIVE ({stale_worker[1]})"
        else:
            bg_status = "INACTIVE"
        print(f"Background Session: {bg_status}")
        return 0 if active_worker else 1

    # Handle --stop
    if args.stop:
        active_worker = find_active_worker_session(
            hpc,
            preferred_username=preferred_username,
            exact_user=exact_user,
            verify_ping=False,
        )
        active_master = find_active_control_master(
            hpc,
            preferred_username=preferred_username,
            exact_user=exact_user,
        )
        resolved_user = (
            (active_worker[0] if active_worker else None)
            or (active_master[0] if active_master else None)
            or preferred_username
        )
        if not resolved_user:
            # Scan any leftover socket/pid files for this cluster even if socket isn't accepting
            suffix = f"_{hpc}.sock"
            for cand in sorted(glob.glob(f"/tmp/hpc_term_*_{hpc}.sock*")):
                base = os.path.basename(cand)
                for ext in (".pid", ".err", ""):
                    if ext and base.endswith(suffix + ext):
                        resolved_user = base[len("hpc_term_") : -len(suffix + ext)]
                        break
                    if not ext and base.endswith(suffix):
                        resolved_user = base[len("hpc_term_") : -len(suffix)]
                        break
                if resolved_user:
                    break
        if not resolved_user:
            print(f"[!] Error: Unable to determine username for '{hpc}'.", file=sys.stderr)
            return 1
        stopped = stop_background_terminal_session(resolved_user, hpc)
        print(
            f"[✓] Stopped background terminal session for {resolved_user}@{hpc}"
            + ("" if stopped else " (worker was not running)")
        )
        return 0

    # If --cmd is passed and a healthy background terminal worker is already running,
    # execute directly without re-checking SSH!
    if args.cmd:
        active_worker = find_active_worker_session(
            hpc,
            preferred_username=preferred_username,
            exact_user=exact_user,
            verify_ping=True,
            probe_remote=True,
        )
        if active_worker:
            return _execute_cli_cmd(active_worker[0], hpc, args.cmd, args.cmd_timeout)
        _cleanup_unresponsive_workers(
            hpc,
            preferred_username=preferred_username,
            exact_user=exact_user,
            probe_first=False,
        )

    # Step 1: Detect whether we already have an existing multiplex SSH session
    active_master = find_active_control_master(
        hpc,
        preferred_username=preferred_username,
        exact_user=exact_user,
    )
    had_existing_master = bool(active_master)
    if active_master:
        username, hpc_address, master_sock = active_master
        print(f"[✓] Using existing multiplex SSH session: {username}@{hpc_address}", file=status_out)
        if hpc not in hpc_config or exact_user:
            update_hpc_config(config_path, hpc, username)
    else:
        if args.cmd and not sys.stdin.isatty():
            print(
                f"[!] Error: No active HPC session for '{hpc}'. Run in your terminal: ush/ssh_helper.py {hpc}",
                file=sys.stderr,
            )
            return 1

        # No active multiplex session: use .hpc_config / CLI username or prompt
        username = preferred_username
        if not username:
            username = prompt_username(hpc)
        elif not clean_cli_user and not args.cmd:
            print(f"[*] Using username '{username}' for '{hpc}' from {config_path.name}.", file=status_out)
        if not username or username == "<your_username>":
            print(f"[!] Error: RDHPCS username is required for '{hpc}'.", file=sys.stderr)
            return 1

        try:
            hpc_address = select_working_address(hpc)
        except (ValueError, ConnectionError) as exc:
            print(f"[!] Connection Error: {exc}", file=sys.stderr)
            return 1

        ok_master, master_sock = establish_master_connection(username, hpc_address, hpc)
        if not ok_master:
            return 1
        update_hpc_config(config_path, hpc, username)

    # Step 2: Ensure persistent background terminal session is running
    use_spinner = bool(not args.cmd and getattr(status_out, "isatty", lambda: False)())

    def _announce_worker_start() -> None:
        if use_spinner:
            print(f"[*] Ensuring persistent background terminal session for {hpc}...", flush=True, file=status_out)
        else:
            print(f"[*] Ensuring persistent background terminal session for {hpc}...", end=" ", flush=True, file=status_out)

    def _announce_worker_result(ok: bool) -> None:
        if use_spinner:
            state = "ACTIVE [✓]" if ok else "FAILED [!]"
            print(f"[{'✓' if ok else '!'}] Background terminal session for {hpc}: {state}", file=status_out)
        else:
            print("ACTIVE [✓]" if ok else "FAILED [!]", file=status_out)

    worker_ready = False
    if had_existing_master:
        max_existing_attempts = max(1, len(HPC_ENDPOINTS.get(hpc, [])))
        for attempt in range(max_existing_attempts):
            if attempt > 0:
                alt_master = find_active_control_master(
                    hpc,
                    preferred_username=preferred_username,
                    exact_user=exact_user,
                )
                if not alt_master:
                    break
                username, hpc_address, master_sock = alt_master
                print(f"[✓] Using existing multiplex SSH session: {username}@{hpc_address}", file=status_out)
                if hpc not in hpc_config or exact_user:
                    update_hpc_config(config_path, hpc, username)

            _announce_worker_start()
            worker_ready = ensure_background_terminal_session(
                username=username,
                hpc_address=hpc_address,
                hpc=hpc,
                master_sock=master_sock,
                idle_timeout=args.idle_timeout,
                first_byte_timeout=args.first_byte_timeout,
                show_spinner=use_spinner,
            )
            if worker_ready:
                break
            _announce_worker_result(False)
            close_control_master(username, hpc_address, sock_path=master_sock)
            print(
                f"[!] Closed stale multiplex SSH session for {username}@{hpc_address}.",
                file=sys.stderr,
            )

        if not worker_ready:
            if not args.cmd and sys.stdin.isatty():
                print(f"[!] Existing multiplex session for {username}@{hpc_address} failed to start a terminal; reconnecting...")
                try:
                    hpc_address = select_working_address(hpc)
                except (ValueError, ConnectionError) as exc:
                    print(f"[!] Connection Error: {exc}", file=sys.stderr)
                    return 1
                ok_master, master_sock = establish_master_connection(username, hpc_address, hpc)
                if not ok_master:
                    return 1
                update_hpc_config(config_path, hpc, username)
                _announce_worker_start()
                worker_ready = ensure_background_terminal_session(
                    username=username,
                    hpc_address=hpc_address,
                    hpc=hpc,
                    master_sock=master_sock,
                    idle_timeout=args.idle_timeout,
                    first_byte_timeout=args.first_byte_timeout,
                    show_spinner=use_spinner,
                )
                if not worker_ready:
                    _announce_worker_result(False)
                    return 1
            else:
                return 1
    else:
        _announce_worker_start()
        worker_ready = ensure_background_terminal_session(
            username=username,
            hpc_address=hpc_address,
            hpc=hpc,
            master_sock=master_sock,
            idle_timeout=args.idle_timeout,
            first_byte_timeout=args.first_byte_timeout,
            show_spinner=use_spinner,
        )
        if not worker_ready:
            _announce_worker_result(False)
            return 1
    _announce_worker_result(True)

    # Step 3: If --cmd was requested, run it on the background terminal session
    if args.cmd:
        return _execute_cli_cmd(username, hpc, args.cmd, args.cmd_timeout)

    print(f"[✓] Ready! Background terminal session active for {username}@{hpc}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
