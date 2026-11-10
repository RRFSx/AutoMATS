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
import fcntl
import glob
import json
import os
import pty
import re
import select
import socket
import struct
import subprocess
import sys
import termios
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


def find_active_worker_session(
    hpc: str,
    preferred_username: Optional[str] = None,
    exact_user: bool = False,
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
        if is_socket_alive(sock_str):
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
    """Removes a dead/stale ControlMaster socket file if present."""
    master_sock = get_master_sock_path(username, host, port)
    if master_sock.exists() and not is_master_alive(username, host, port, master_sock):
        try:
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

            try:
                sock_path.unlink()
            except OSError:
                pass

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
        "-o", "ServerAliveCountMax=60",
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


def _set_pty_winsize(fd: int, rows: int = 24, cols: int = 32768) -> None:
    """Sets wide PTY dimensions to prevent line-wrapping of long commands."""
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


def _write_all_pty(master_fd: int, data: bytes, timeout: float = 30.0) -> bytes:
    """
    Writes `data` to `master_fd` in small chunks while draining output so the
    Linux canonical PTY input buffer (4096 bytes) never deadlocks.
    Returns any output bytes drained while writing.
    """
    drained = bytearray()
    view = memoryview(data)
    offset = 0
    chunk_size = 1024
    deadline = time.time() + timeout

    while offset < len(view):
        if time.time() > deadline:
            raise TimeoutError("Timed out writing command input to background terminal session.")
        end = min(offset + chunk_size, len(view))
        try:
            written = os.write(master_fd, view[offset:end])
            offset += written
        except BlockingIOError:
            pass

        r, _, _ = select.select([master_fd], [], [], 0.005)
        if master_fd in r:
            try:
                out = os.read(master_fd, 65536)
                if not out:
                    raise EOFError("Remote terminal session closed unexpectedly.")
                drained.extend(out)
            except BlockingIOError:
                pass

    return bytes(drained)


def _execute_on_pty(
    master_fd: int,
    cmd: str,
    stdin_bytes: bytes = b"",
    timeout: float = 120.0,
) -> Tuple[int, bytes]:
    """
    Executes `cmd` on the live remote PTY session with lossless binary stdin/stdout.
    Uses base64 framing so binary data (such as `tar -P -cf - --null -T -`) and
    arbitrary null-separated file lists pass cleanly without PTY line-discipline
    corruption.
    """
    token = uuid.uuid4().hex[:12]
    start_marker = f"__HPC_S_{token}__".encode("ascii")
    end_prefix = f"__HPC_E_{token}__:".encode("ascii")

    buf = bytearray()
    deadline = time.time() + max(float(timeout), 5.0)

    if stdin_bytes:
        eof_tag = f"__IN_{token}__"
        b64_in = base64.b64encode(stdin_bytes).decode("ascii")
        lines = [b64_in[i : i + 76] for i in range(0, len(b64_in), 76)]
        header = (
            f"printf '{start_marker.decode()}\\n'; "
            f"base64 -d << '{eof_tag}' | ( {cmd} ) | base64 | tr -d '\\r\\n'; "
            f"printf '\\n{end_prefix.decode()}%s\\n' \"${{PIPESTATUS[1]:-$?}}\"\n"
        ).encode("utf-8")
        body = ("\n".join(lines) + f"\n{eof_tag}\n").encode("ascii")
        drained = _write_all_pty(master_fd, header + body, timeout=max(float(timeout), 30.0))
        buf.extend(drained)
    else:
        command_line = (
            f"printf '{start_marker.decode()}\\n'; "
            f"( {cmd} ) | base64 | tr -d '\\r\\n'; "
            f"printf '\\n{end_prefix.decode()}%s\\n' \"${{PIPESTATUS[0]:-$?}}\"\n"
        ).encode("utf-8")
        drained = _write_all_pty(master_fd, command_line, timeout=max(float(timeout), 30.0))
        buf.extend(drained)

    while time.time() < deadline:
        s_idx = buf.find(start_marker)
        if s_idx != -1:
            e_idx = buf.find(end_prefix, s_idx + len(start_marker))
            if e_idx != -1:
                nl_idx = buf.find(b"\n", e_idx + len(end_prefix))
                if nl_idx != -1:
                    rc_bytes = bytes(buf[e_idx + len(end_prefix) : nl_idx]).strip()
                    try:
                        rc = int(rc_bytes.decode("ascii"))
                    except ValueError:
                        rc = 1

                    raw_payload = bytes(buf[s_idx + len(start_marker) : e_idx])
                    b64_clean = re.sub(rb"[^A-Za-z0-9+/=]", b"", raw_payload)
                    stdout_bytes = base64.b64decode(b64_clean) if b64_clean else b""
                    return rc, stdout_bytes

        remaining = max(0.05, min(0.5, deadline - time.time()))
        r, _, _ = select.select([master_fd], [], [], remaining)
        if master_fd in r:
            try:
                chunk = os.read(master_fd, 65536)
                if not chunk:
                    raise EOFError("Remote terminal session closed during command execution.")
                buf.extend(chunk)
            except BlockingIOError:
                pass

    raise TimeoutError(f"Command timed out after {timeout}s on background terminal session.")


def run_worker_server(
    sock_path: str,
    master_ssh_sock: str,
    user: str,
    host: str,
    port: int = 22,
    default_node: Optional[str] = None,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
) -> None:
    """
    Maintains a persistent background interactive PTY session to the HPC headnode.
    Listens on `sock_path` and executes commands (including binary `tar`) over
    the live terminal session while keeping it alive permanently.
    """
    master, slave = pty.openpty()
    _set_pty_winsize(master, rows=24, cols=32768)
    _set_pty_winsize(slave, rows=24, cols=32768)

    pid = os.fork()
    if pid == 0:
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
            "-o", "ServerAliveCountMax=60",
            "-S", str(master_ssh_sock),
        ]
        if port and int(port) != 22:
            ssh_cmd.extend(["-p", str(port)])
        ssh_cmd.append(f"{user}@{host}")
        os.execlp("ssh", *ssh_cmd)
    os.close(slave)

    # Intercept bastion countdown (if any) and wait for headnode shell prompt
    buf = b""
    t0 = time.time()
    sent_c = False
    sent_node = False
    ready = False
    is_bastion = is_bastion_host(host)
    deadline = t0 + 45.0

    while time.time() < deadline:
        r, _, _ = select.select([master], [], [], 0.05)
        if master in r:
            try:
                chunk = os.read(master, 4096)
                if not chunk:
                    break
                buf += chunk
                low_buf = buf.lower()

                if is_bastion:
                    if not sent_c and (b"hit ^c" in low_buf or b"within 5 seconds" in low_buf):
                        time.sleep(0.02)
                        os.write(master, b"\x03")
                        sent_c = True

                    if sent_c and not sent_node and (
                        b"enter a hostname" in low_buf
                        or b"hostname [" in low_buf
                        or b"enter a host" in low_buf
                    ):
                        time.sleep(0.02)
                        node_str = (default_node or "").strip()
                        os.write(master, f"{node_str}\n".encode() if node_str else b"\n")
                        sent_node = True

                    ready_to_check = sent_node or (not sent_c and time.time() - t0 > 2.5)
                else:
                    ready_to_check = True

                if ready_to_check and not ready:
                    raw_clean = buf.decode("utf-8", errors="ignore")
                    clean = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", raw_clean)
                    clean = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", clean)
                    clean = re.sub(r"\x1b[\(\)][A-Za-z0-9]", "", clean)
                    if PROMPT_REGEX.search(clean.strip()):
                        ready = True
                        break
            except OSError:
                break

    if not ready:
        try:
            os.close(master)
        except OSError:
            pass
        return

    # Disable terminal echo on the remote PTY so command input does not echo back
    try:
        os.write(master, b"stty -echo 2>/dev/null || true\n")
        time.sleep(0.1)
        while True:
            r_drain, _, _ = select.select([master], [], [], 0.05)
            if master not in r_drain:
                break
            if not os.read(master, 4096):
                break
    except OSError:
        pass

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
    except Exception:
        try:
            os.close(master)
        except OSError:
            pass
        return

    last_active = time.time()
    last_keepalive = time.time()

    try:
        while True:
            r, _, _ = select.select([srv, master], [], [], 5.0)
            if master in r:
                try:
                    c = os.read(master, 4096)
                    if not c:
                        break
                except OSError:
                    break

            if srv in r:
                conn, _ = srv.accept()
                last_active = time.time()
                try:
                    raw = bytearray()
                    while b"\n" not in raw:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        raw.extend(chunk)
                    if not raw:
                        conn.close()
                        continue

                    req = json.loads(raw.decode("utf-8").strip())
                    cmd_to_run = req.get("cmd", "")

                    if cmd_to_run == "exit":
                        conn.sendall(json.dumps({"code": 0, "stdout_b64": ""}).encode("utf-8") + b"\n")
                        conn.close()
                        break

                    if cmd_to_run == "ping" or not cmd_to_run:
                        conn.sendall(json.dumps({"code": 0, "stdout_b64": ""}).encode("utf-8") + b"\n")
                        conn.close()
                        continue

                    stdin_b64 = req.get("stdin_b64", "")
                    stdin_bytes = base64.b64decode(stdin_b64) if stdin_b64 else b""
                    cmd_timeout = float(req.get("timeout", 120.0))

                    rc, out_bytes = _execute_on_pty(
                        master,
                        cmd=cmd_to_run,
                        stdin_bytes=stdin_bytes,
                        timeout=cmd_timeout,
                    )
                    resp = {
                        "code": rc,
                        "stdout_b64": base64.b64encode(out_bytes).decode("ascii"),
                    }
                    conn.sendall(json.dumps(resp).encode("utf-8") + b"\n")
                except Exception as exc:
                    err_resp = {
                        "code": 1,
                        "stdout_b64": "",
                        "error": str(exc),
                    }
                    try:
                        conn.sendall(json.dumps(err_resp).encode("utf-8") + b"\n")
                    except OSError:
                        pass
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass

            now = time.time()
            # Keep remote shell $TMOUT alive with a periodic no-op comment every 60s
            if now - last_keepalive >= 60.0:
                try:
                    os.write(master, b"# keepalive\n")
                    last_keepalive = now
                except OSError:
                    break

            if idle_timeout > 0 and (now - last_active > idle_timeout):
                break
    finally:
        try:
            os.write(master, b"exit\n")
            time.sleep(0.05)
            os.close(master)
        except OSError:
            pass
        if os.path.exists(sock_path):
            try:
                os.unlink(sock_path)
            except OSError:
                pass
        srv.close()


def spawn_worker(
    sock_path: str,
    master_ssh_sock: str,
    user: str,
    host: str,
    port: int = 22,
    default_node: Optional[str] = None,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
) -> None:
    """Spawns the persistent background terminal worker daemon."""
    if os.path.exists(sock_path):
        try:
            os.unlink(sock_path)
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
    ]
    if default_node:
        cmd.extend(["--default-node", default_node])

    pid = os.fork()
    if pid == 0:
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        if devnull > 2:
            os.close(devnull)
        d_pid = os.fork()
        if d_pid == 0:
            os.execv(python_exe, cmd)
        os._exit(0)
    os.waitpid(pid, 0)


def ensure_background_terminal_session(
    username: str,
    hpc_address: str,
    hpc: str,
    master_sock: Optional[Path] = None,
    default_node: Optional[str] = None,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
) -> bool:
    """
    Ensures the persistent background terminal session is running for `hpc`.
    Reuses an existing session if already active.
    """
    worker_sock = get_worker_sock_path(username, hpc)
    if is_socket_alive(worker_sock):
        return True

    node = default_node or DEFAULT_HEAD_NODES.get(hpc.lower())
    m_sock = str(master_sock or get_master_sock_path(username, hpc_address, 22))

    spawn_worker(
        sock_path=worker_sock,
        master_ssh_sock=m_sock,
        user=username,
        host=hpc_address,
        default_node=node,
        idle_timeout=idle_timeout,
    )

    deadline = time.time() + 55.0
    while time.time() < deadline:
        if is_socket_alive(worker_sock):
            return True
        time.sleep(0.25)

    print(
        f"[!] Error: Background terminal session for '{hpc}' did not become ready within 55s.",
        file=sys.stderr,
    )
    return False


def run_remote_command(
    username: Optional[str],
    hpc: str,
    cmd: str,
    stdin_bytes: bytes = b"",
    timeout: float = 120.0,
) -> Tuple[int, bytes, str]:
    """
    Executes `cmd` (with optional binary `stdin_bytes`) through the persistent
    background terminal session for `hpc`.
    If `username` is omitted/empty, automatically discovers the active worker
    session for `hpc`.
    Returns (exit_code, stdout_bytes, error_message).
    """
    pref_user = _clean_config_username(username) or load_hpc_config().get(hpc.lower())
    active = find_active_worker_session(hpc, preferred_username=pref_user)
    if not active:
        who = f"{username}@{hpc}" if username else hpc
        return 1, b"", f"Background terminal session is not running for {who}. Run: ush/ssh_helper.py {hpc}"

    resolved_user, worker_sock = active
    payload = {
        "cmd": cmd,
        "stdin_b64": base64.b64encode(stdin_bytes).decode("ascii") if stdin_bytes else "",
        "timeout": timeout,
    }
    raw_req = json.dumps(payload).encode("utf-8") + b"\n"

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout + 10.0)
            s.connect(worker_sock)
            s.sendall(raw_req)
            resp_buf = bytearray()
            while b"\n" not in resp_buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                resp_buf.extend(chunk)
    except OSError as exc:
        return 1, b"", f"Failed communicating with background terminal worker ({resolved_user}): {exc}"

    if not resp_buf:
        return 1, b"", "Empty response from background terminal worker."

    try:
        resp = json.loads(resp_buf.decode("utf-8").strip())
    except json.JSONDecodeError as exc:
        return 1, b"", f"Malformed response from background terminal worker: {exc}"

    rc = int(resp.get("code", 1))
    out_b64 = resp.get("stdout_b64", "")
    stdout_bytes = base64.b64decode(out_b64) if out_b64 else b""
    err_msg = str(resp.get("error", ""))
    return rc, stdout_bytes, err_msg


def stop_background_terminal_session(username: str, hpc: str) -> bool:
    """Cleanly stops the background terminal session for `(username, hpc)`."""
    worker_sock = get_worker_sock_path(username, hpc)
    if not os.path.exists(worker_sock):
        return False
    if is_socket_alive(worker_sock):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(3.0)
                s.connect(worker_sock)
                s.sendall(b'{"cmd": "exit"}\n')
                s.recv(1024)
        except OSError:
            pass
    if os.path.exists(worker_sock):
        try:
            os.unlink(worker_sock)
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

    return parser.parse_args(argv)


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
        )
        return 0

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
        )
        resolved_user = (
            (active_worker[0] if active_worker else None)
            or (active_master[0] if active_master else None)
            or preferred_username
            or "<unknown>"
        )
        print(f"Cluster:            {hpc}")
        print(f"Username:           {resolved_user}")
        print(f"Multiplex Session:  {'ACTIVE (' + active_master[1] + ')' if active_master else 'INACTIVE'}")
        print(f"Background Session: {'ACTIVE (' + active_worker[1] + ')' if active_worker else 'INACTIVE'}")
        return 0 if active_worker else 1

    # Handle --stop
    if args.stop:
        active_worker = find_active_worker_session(
            hpc,
            preferred_username=preferred_username,
            exact_user=exact_user,
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
            print(f"[!] Error: Unable to determine username for '{hpc}'.", file=sys.stderr)
            return 1
        stopped = stop_background_terminal_session(resolved_user, hpc)
        print(
            f"[✓] Stopped background terminal session for {resolved_user}@{hpc}"
            + ("" if stopped else " (worker was not running)")
        )
        return 0

    # If --cmd is passed and an active background terminal worker is already running,
    # execute directly without re-checking SSH!
    if args.cmd:
        active_worker = find_active_worker_session(
            hpc,
            preferred_username=preferred_username,
            exact_user=exact_user,
        )
        if active_worker:
            stdin_bytes = b""
            if not sys.stdin.isatty():
                try:
                    stdin_bytes = sys.stdin.buffer.read()
                except Exception:
                    stdin_bytes = b""
            rc, out_bytes, err_msg = run_remote_command(
                username=active_worker[0],
                hpc=hpc,
                cmd=args.cmd,
                stdin_bytes=stdin_bytes,
            )
            if out_bytes:
                sys.stdout.buffer.write(out_bytes)
                sys.stdout.buffer.flush()
            if rc != 0 and err_msg:
                print(f"[!] {err_msg}", file=sys.stderr)
            return rc

    # Step 1: Detect whether we already have an existing multiplex SSH session
    active_master = find_active_control_master(
        hpc,
        preferred_username=preferred_username,
        exact_user=exact_user,
    )
    if active_master:
        username, hpc_address, master_sock = active_master
        print(f"[✓] Using existing multiplex SSH session: {username}@{hpc_address}", file=status_out)
        if hpc not in hpc_config or exact_user:
            update_hpc_config(config_path, hpc, username)
    else:
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
    print(f"[*] Ensuring persistent background terminal session for {hpc}...", end=" ", flush=True, file=status_out)
    if not ensure_background_terminal_session(
        username=username,
        hpc_address=hpc_address,
        hpc=hpc,
        master_sock=master_sock,
        idle_timeout=args.idle_timeout,
    ):
        print("FAILED [!]", file=status_out)
        return 1
    print("ACTIVE [✓]", file=status_out)

    # Step 3: If --cmd was requested, run it on the background terminal session
    if args.cmd:
        stdin_bytes = b""
        if not sys.stdin.isatty():
            try:
                stdin_bytes = sys.stdin.buffer.read()
            except Exception:
                stdin_bytes = b""
        rc, out_bytes, err_msg = run_remote_command(
            username=username,
            hpc=hpc,
            cmd=args.cmd,
            stdin_bytes=stdin_bytes,
        )
        if out_bytes:
            sys.stdout.buffer.write(out_bytes)
            sys.stdout.buffer.flush()
        if rc != 0 and err_msg:
            print(f"[!] {err_msg}", file=sys.stderr)
        return rc

    print(f"[✓] Ready! Background terminal session active for {username}@{hpc}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
