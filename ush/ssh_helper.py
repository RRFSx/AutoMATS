#!/usr/bin/env python3
"""
ssh_helper.py: Automated SSH Tunnel & Multiplexing Tool for AutoMATS.
  - Establishes and verifies the SSH ControlMaster port-forwarding tunnel
    so AutoMATS can copy files from HPC using 'scp'
Steps:
  1. Ensures SSH ControlMaster configuration is active in ~/.ssh/config.
  2. Checks if local port forward (localhost:<my_local_port>) is ALREADY active:
     - If ACTIVE: Confirms tunnel readiness for SCP transfers and exits immediately (0.1s).
  3. If INACTIVE (tunnel not running):
     - Uses recorded ports from .hpc_config (or discovers assigned remote port on first run).
     - Prompts user to log in once to establish the master tunnel (push USB key, then exit).
     - Verifies tunnel is listening on localhost:<my_local_port> and exits with ready status.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import pty
import re
import select
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Auto-re-exec with local venv python if invoked by system python
ROOT = Path(__file__).resolve().parent.parent
venv_dir = ROOT / ".venv"
venv_python = venv_dir / "bin" / "python"
if venv_python.is_file() and Path(sys.prefix).resolve() != venv_dir.resolve():
    try:
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve())] + sys.argv[1:])
    except OSError:
        pass

# Default cluster endpoint configurations (priority ordered)
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
}

DEFAULT_HEAD_NODES: Dict[str, str] = {
    "gaeac5": "gaea51",
    "gaeac6": "gaea61",
    "gaeac7": "gaea71",
    "ursa": "ufe04",
    "hera": "hfe01",
}

PROMPT_REGEX = re.compile(r'\[.*?@?[\w\.\-]+.*?\]\s*[\$#>%]\s*$|[\w\.\-]+[^\n\r]*[\$#>%]\s*$|[\$#>%]\s*$')

PORT_DETECTION_PATTERNS = [
    re.compile(r"(?:assigned|allocated|tunnel|forwarded|remote|local)\s+port[^\d\n]*?(\d{4,5})", re.IGNORECASE),
    re.compile(r"port\s+(\d{4,5})\s+(?:is\s+)?(?:assigned|allocated|forwarded)", re.IGNORECASE),
    re.compile(r"port\s*[:=]\s*(\d{4,5})", re.IGNORECASE),
    re.compile(r"localhost\s*:\s*(\d{4,5})", re.IGNORECASE),
    re.compile(r"-L\s*(\d{4,5}):", re.IGNORECASE),
    re.compile(r"\b(4\d{4})\b"),
]

SSH_CONFIG_BLOCK = """Host *  !*github.com
    ServerAliveInterval 60
    ControlMaster auto
    ControlPath ~/.ssh/master-%r@%h:%p
    ControlPersist yes
"""


def get_default_config_path() -> Path:
    """Finds .hpc_config path in CWD or AutoMATS root directory."""
    cwd_cfg = Path.cwd() / ".hpc_config"
    if cwd_cfg.exists():
        return cwd_cfg

    script_dir = Path(__file__).resolve().parent
    automats_root = script_dir.parent if script_dir.name == "ush" else script_dir
    automats_cfg = automats_root / ".hpc_config"
    if automats_cfg.exists():
        return automats_cfg

    return automats_cfg if automats_root.exists() else cwd_cfg


# ==============================================================================
# Port Calculation, Banner Detection & Multiplexing Infrastructure
# ==============================================================================

DEFAULT_IDLE_TIMEOUT = 10800  # 3 hours


def is_bastion_host(host: str) -> bool:
    """Checks if host is a NOAA RDHPCS bastion/gateway."""
    return ".rdhpcs.noaa.gov" in host or "-mfa." in host or "-cac." in host or "bastion" in host


def get_worker_sock_path(user: str, cluster_name: str) -> str:
    """Path to the background interactive shell worker socket."""
    return f"/tmp/hpc_worker_{user}_{cluster_name.lower()}.sock"


def is_socket_alive(socket_path: str) -> bool:
    """Checks whether a UNIX domain socket is active and accepting connections."""
    if not os.path.exists(socket_path):
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(socket_path)
        s.close()
        return True
    except (socket.error, OSError):
        return False


def compute_local_port(hpc: str, remote_port: int) -> int:
    """
    Computes a conflict-free local forwarding port for the Mac localhost.
    - TCP port numbers are 16-bit unsigned integers: valid range is 1 to 65535.
        * ursa: Uses remote_port directly
        * gaeac6: Replaces the last digit with '6'
        * gaeac7: Replaces the last digit with '7'
      This cleanly avoids local port collisions between gaeac6 and gaeac7 on localhost
    """
    hpc_key = hpc.lower()

    if hpc_key == "ursa":
        return remote_port

    port_str = str(remote_port)
    if hpc_key == "gaeac6":
        mapped_str = port_str[:-1] + "6"
        mapped_port = int(mapped_str)
        return mapped_port

    if hpc_key == "gaeac7":
        mapped_str = port_str[:-1] + "7"
        mapped_port = int(mapped_str)
        return mapped_port

    return remote_port


def extract_port_from_text(text: str) -> Optional[int]:
    """Scans text for assigned remote port forwarding numbers."""
    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    for p in PORT_DETECTION_PATTERNS:
        for m in p.finditer(clean):
            try:
                val = int(m.group(1))
                if 1024 <= val <= 65535 and val != 22:
                    return val
            except (ValueError, IndexError):
                pass
    return None


def detect_port_from_ssh_config(hpc: str) -> Optional[int]:
    """Inspects ~/.ssh/config for Host directives matching cluster (e.g. Host Ursa, Host Gaea)."""
    config_file = Path.home() / ".ssh" / "config"
    if not config_file.exists():
        return None
    try:
        content = config_file.read_text(encoding="utf-8")
    except OSError:
        return None

    aliases = [hpc.lower()]
    if hpc.lower() in ("gaeac5", "gaeac6", "gaeac7"):
        aliases.extend(["gaea", "gaea-c5", "gaea-c6", "gaea-c7"])

    current_hosts: List[str] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("host "):
            current_hosts = [h.lower() for h in line[5:].split()]
        elif current_hosts and line.lower().startswith("port "):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                port = int(parts[1])
                for alias in aliases:
                    if alias in current_hosts and 1024 <= port <= 65535 and port != 22:
                        return port
    return None


def detect_username_from_control_master(hpc: str) -> Optional[str]:
    """Extracts username from existing ControlMaster sockets for the target cluster in ~/.ssh/master-*."""
    ssh_dir = Path.home() / ".ssh"
    if not ssh_dir.exists() or not hpc:
        return None

    hpc_key = hpc.lower()
    endpoints = HPC_ENDPOINTS.get(hpc_key, [])
    for host in endpoints:
        for sock in glob.glob(str(ssh_dir / f"master-*@{host}:*")):
            base = os.path.basename(sock)
            m = re.match(r"^master-(.+?)@", base)
            if m:
                return m.group(1)

    return None


def resolve_username(
    hpc: str,
    requested_username: Optional[str] = None,
    config: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[str]:
    """
    Resolves username strictly for the target HPC cluster:
      1. Explicit CLI argument.
      2. Target cluster's own entry in .hpc_config.
      3. Existing ControlMaster socket for the target cluster (master-%r@{host}:*).
      4. Interactive prompt fallback.
    """
    # 1. Explicit CLI argument
    if requested_username and requested_username != "<your_username>":
        return requested_username

    # 2. Target cluster's own entry in .hpc_config
    if config:
        cluster_user = config.get(hpc.lower(), {}).get("username")
        if cluster_user and cluster_user != "<your_username>":
            return cluster_user

    # 3. Existing ControlMaster socket strictly for the target cluster
    user_from_master = detect_username_from_control_master(hpc)
    if user_from_master:
        return user_from_master

    # 4. Prompt user as fallback
    try:
        user_input = input(f"Enter username for '{hpc}': ").strip()
        if user_input:
            return user_input
    except (EOFError, KeyboardInterrupt):
        pass

    return None


def detect_port_from_file(username: str, hpc: str) -> Optional[int]:
    """Reads port detected during background session startup from /tmp file."""
    port_file = Path(f"/tmp/hpc_detected_port_{username}_{hpc.lower()}.txt")
    if port_file.exists():
        try:
            val = int(port_file.read_text(encoding="utf-8").strip())
            if 1024 <= val <= 65535 and val != 22:
                return val
        except Exception:
            pass
    return None


def query_worker_for_port(worker_sock: str, timeout: float = 2.0) -> Optional[int]:
    """Queries live interactive worker session for remote tunnel port."""
    if not is_socket_alive(worker_sock):
        return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(worker_sock)
        cmd = 'echo __TUNNEL_PORT__:${PORT:-$(env | grep -iE "port|tunnel" | head -n1)}:__END__'
        payload = json.dumps({"cmd": cmd, "timeout": int(timeout)}) + "\n"
        s.sendall(payload.encode("utf-8"))
        data = b""
        while b"\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        if data:
            res = json.loads(data.decode("utf-8").strip())
            return extract_port_from_text(res.get("output", ""))
    except Exception:
        pass
    return None


def detect_remote_port(
    hpc: str,
    username: str,
    worker_sock: Optional[str] = None,
) -> Optional[int]:
    """
    Auto-detects the assigned remote local port across multiple sources:
      1. Local ~/.ssh/config host directives (e.g. Host Ursa, Host Gaea).
      2. Background session startup detection file.
      3. Live interactive worker socket query.
    """
    # 1. ~/.ssh/config
    port = detect_port_from_ssh_config(hpc)
    if port:
        return port

    # 2. Worker startup port file
    port = detect_port_from_file(username, hpc)
    if port:
        return port

    # 3. Live worker session query
    sock = worker_sock or get_worker_sock_path(username, hpc)
    if is_socket_alive(sock):
        port = query_worker_for_port(sock)
        if port:
            return port

    return None


# ==============================================================================
# Self-Contained Config Management (No external PyYAML dependency)
# ==============================================================================

def load_hpc_config(path: Path) -> Dict[str, Dict[str, Any]]:
    """Loads .hpc_config without external dependencies."""
    if not path.exists():
        return {}
    cfg: Dict[str, Dict[str, Any]] = {}
    current_cluster = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith(":") and not ":" in line[:-1]:
            current_cluster = line[:-1].strip().lower()
            cfg[current_cluster] = {}
        elif current_cluster and ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()
            if val.lower() in ("null", "none", "~", ""):
                parsed_val = None
            elif val.isdigit():
                parsed_val = int(val)
            else:
                parsed_val = val
            cfg[current_cluster][key] = parsed_val
    return cfg


def save_hpc_config(path: Path, data: Dict[str, Dict[str, Any]]) -> None:
    """Saves dictionary to .hpc_config without external dependencies."""
    lines = []
    for cluster, entry in data.items():
        lines.append(f"{cluster}:")
        for k, v in entry.items():
            val_str = "null" if v is None else str(v)
            lines.append(f"  {k}: {val_str}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def create_template_hpc_config(
    path: Path,
    username: Optional[str] = None,
    target_cluster: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Creates a template .hpc_config only for the target cluster being accessed."""
    print(f"[*] Configuration file '{path.name}' not found.")
    user = username
    if not user:
        prompt_name = f" for {target_cluster}" if target_cluster else ""
        try:
            user = input(f"Enter RDHPCS username{prompt_name}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            user = ""
    if not user:
        user = "<your_username>"

    template_data: Dict[str, Dict[str, Any]] = {}
    clusters = [target_cluster.lower()] if target_cluster else list(HPC_ENDPOINTS.keys())
    for cluster in clusters:
        detected_rem = detect_remote_port(cluster, user) if user and user != "<your_username>" else None
        detected_loc = compute_local_port(cluster, detected_rem) if detected_rem else None
        template_data[cluster] = {
            "username": user,
            "my_local_port": detected_loc,
            "remote_local_port": detected_rem,
        }

    save_hpc_config(path, template_data)
    print(f"[+] Created configuration file '{path.name}'.\n")

    # Ensure config file is not tracked by git
    gitignore_path = path.parent / ".gitignore"
    if gitignore_path.exists():
        try:
            content = gitignore_path.read_text(encoding="utf-8")
            if path.name not in content:
                sep = "\n" if not content.endswith("\n") else ""
                gitignore_path.write_text(content + sep + f"{path.name}\n", encoding="utf-8")
        except OSError:
            pass

    return template_data


def sync_missing_ports_in_config(
    path: Path,
    username: Optional[str] = None,
    target_cluster: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Auto-detects and fills in any null ports for clusters in .hpc_config."""
    if not path.exists():
        return {}
    cfg = load_hpc_config(path)
    modified = False
    clusters = [target_cluster.lower()] if target_cluster and target_cluster.lower() in cfg else list(cfg.keys())
    for cluster in clusters:
        entry = cfg.get(cluster)
        if not entry:
            continue
        user = username or entry.get("username")
        if not user or user == "<your_username>":
            continue
        rem = entry.get("remote_local_port")
        loc = entry.get("my_local_port")
        if rem is None:
            detected_rem = detect_remote_port(cluster, user)
            if detected_rem is not None:
                entry["remote_local_port"] = detected_rem
                rem = detected_rem
                modified = True
        if loc is None and rem is not None:
            loc = compute_local_port(cluster, rem)
            entry["my_local_port"] = loc
            modified = True
    if modified:
        save_hpc_config(path, cfg)
    return cfg


def update_hpc_config(
    path: Path,
    hpc: str,
    username: str,
    my_local_port: int,
    remote_local_port: int,
) -> None:
    """Update or insert a specific HPC section in .hpc_config only when missing or changed."""
    cfg = load_hpc_config(path)
    current = cfg.get(hpc.lower(), {})
    if (
        current.get("username") == username
        and current.get("my_local_port") == my_local_port
        and current.get("remote_local_port") == remote_local_port
    ):
        return  # Already configured; do not overwrite or modify existing entry

    cfg[hpc.lower()] = {
        "username": username,
        "my_local_port": my_local_port,
        "remote_local_port": remote_local_port,
    }
    save_hpc_config(path, cfg)
    print(f"[✓] Saved configuration for '{hpc}' to {path.name}")


# ==============================================================================
# SSH Config Management
# ==============================================================================

def ensure_ssh_config(config_path: Optional[Path] = None) -> bool:
    """
    Ensures SSH ControlMaster and multiplexing directives are in ~/.ssh/config.
    Returns True if file was created or updated, False if already configured.
    """
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
        print(f"[+] Created {config_file} with ControlMaster configuration.")
        return True

    content = config_file.read_text(encoding="utf-8")

    # Check whether ControlPath and ControlMaster are already configured
    has_control_path = bool(re.search(r"ControlPath\s+~/\.ssh/master-%r@%h:%p", content))
    has_control_master = bool(re.search(r"ControlMaster\s+auto", content))

    if has_control_path and has_control_master:
        return False

    # Append block
    separator = "\n" if not content.endswith("\n") else ""
    config_file.write_text(content + separator + SSH_CONFIG_BLOCK, encoding="utf-8")
    try:
        os.chmod(config_file, 0o600)
    except OSError:
        pass
    print(f"[+] Appended ControlMaster multiplexing configuration to {config_file}.")
    return True


# ==============================================================================
# Network Reachability and Endpoint Resolution
# ==============================================================================

def is_port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    """Test TCP connection to host:port with timeout."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except (socket.timeout, socket.error, OSError):
        return False


def is_local_port_in_use(port: int) -> bool:
    """Checks if a local port is already bound on localhost without connecting."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def select_working_address(hpc: str) -> str:
    """
    Selects working HPC address from configured priority list.
    Tries each address in order; errors out if none respond.
    """
    hpc_key = hpc.lower()
    endpoints = HPC_ENDPOINTS.get(hpc_key)
    if not endpoints:
        raise ValueError(
            f"Unsupported HPC cluster '{hpc}'. Supported options: {list(HPC_ENDPOINTS.keys())}"
        )

    print(f"[*] Ping '{hpc}' endpoints...")
    for idx, addr in enumerate(endpoints, 1):
        print(f"    [{idx}/{len(endpoints)}] Checking {addr}:22 ...", end=" ", flush=True)
        if is_port_open(addr, 22, timeout=3.0):
            print("ONLINE")
            return addr
        else:
            print("UNREACHABLE / TIMED OUT")

    raise ConnectionError(
        f"Unable to connect to any endpoint for cluster '{hpc}': {endpoints}.\n"
        "Please verify your internet connection, VPN, or DNS settings."
    )



# ==============================================================================
# Port Forwarding Tunnel Setup & Lifecycle
# ==============================================================================

def cleanup_stale_master(username: str, host: str, port: int = 22) -> None:
    """Silently cleans up any dead or stale ControlMaster socket."""
    sock_path = Path.home() / ".ssh" / f"master-{username}@{host}:{port}"
    if sock_path.exists():
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.3)
            s.connect(str(sock_path))
            s.close()
        except Exception:
            try:
                sock_path.unlink()
            except OSError:
                pass


def cleanup_stale_port_forward(port: int) -> None:
    """Terminates any stale process occupying the local forwarding port."""
    try:
        res = subprocess.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            pids = [int(p) for p in res.stdout.strip().split() if p.isdigit()]
            for pid in pids:
                try:
                    os.kill(pid, 15)  # SIGTERM
                except (ProcessLookupError, PermissionError):
                    pass
            time.sleep(0.3)
            for pid in pids:
                try:
                    os.kill(pid, 0)
                    os.kill(pid, 9)  # SIGKILL if still running
                except (ProcessLookupError, PermissionError):
                    pass
            time.sleep(0.1)
    except Exception:
        pass


def is_master_alive(username: str, host: str, port: int = 22) -> bool:
    """Checks whether an active ControlMaster socket exists and is responsive."""
    sock_path = Path.home() / ".ssh" / f"master-{username}@{host}:{port}"
    if not sock_path.exists():
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(str(sock_path))
        s.close()
        return True
    except Exception:
        return False


def cleanup_local_known_hosts(port: int) -> None:
    """Removes stale localhost:<port> key from ~/.ssh/known_hosts to avoid host key warnings."""
    try:
        subprocess.run(
            ["ssh-keygen", "-R", f"[localhost]:{port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def test_local_access(username: str, local_port: int, timeout: float = 3.0) -> bool:
    """Tests whether we can actually access through the local port."""
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={int(timeout)}",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "StrictHostKeyChecking=no",
        "-o", "LogLevel=ERROR",
        "-p", str(local_port),
        f"{username}@localhost",
        "true",
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        return res.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False



def run_worker_server(
    sock_path: str,
    master_ssh_sock: str,
    user: str,
    host: str,
    port: int = 22,
    default_node: Optional[str] = None,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    detected_port_file: Optional[str] = None,
) -> None:
    """
    Maintains a persistent background interactive PTY session to the remote headnode.
    Listens on sock_path and keeps the headnode connection active.
    """
    master, slave = pty.openpty()
    pid = os.fork()
    if pid == 0:
        os.close(master)
        os.dup2(slave, 0)
        os.dup2(slave, 1)
        os.dup2(slave, 2)
        os.close(slave)
        ssh_cmd = ["ssh", "-tt", "-o", "BatchMode=yes", "-o", "ControlMaster=no", "-S", str(master_ssh_sock)]
        if port and int(port) != 22:
            ssh_cmd.extend(["-p", str(port)])
        ssh_cmd.append(f"{user}@{host}")
        os.execlp("ssh", *ssh_cmd)
    os.close(slave)

    # Intercept bastion countdown & land on default_node, or land directly on shell
    buf = b""
    t0 = time.time()
    sent_c = False
    sent_node = False
    ready = False
    is_bastion = is_bastion_host(host)
    deadline = t0 + 45

    while time.time() < deadline:
        r, _, _ = select.select([master], [], [], 0.05)
        if master in r:
            try:
                chunk = os.read(master, 4096)
                if not chunk:
                    break
                buf += chunk

                if is_bastion:
                    if not sent_c and (b"hit ^C" in buf or b"within 5 seconds" in buf):
                        time.sleep(0.02)
                        os.write(master, b"\x03")
                        sent_c = True

                    if sent_c and not sent_node and (b"Enter a hostname" in buf or b"hostname [" in buf):
                        time.sleep(0.02)
                        node_str = (default_node or "").strip()
                        os.write(master, f"{node_str}\n".encode() if node_str else b"\n")
                        sent_node = True

                    ready_to_check = sent_node or (not sent_c and time.time() - t0 > 6)
                else:
                    ready_to_check = True

                if ready_to_check and not ready:
                    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", buf.decode("utf-8", errors="ignore"))
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

    # Extract assigned remote port from session output if requested
    if detected_port_file:
        raw_text = buf.decode("utf-8", errors="ignore")
        detected = extract_port_from_text(raw_text)
        if not detected:
            try:
                os.write(master, b"echo __PORT__:${PORT:-$(env | grep -iE 'port|tunnel' | head -n1)}:__END__\n")
                time.sleep(0.3)
                r_d, _, _ = select.select([master], [], [], 0.5)
                if master in r_d:
                    cmd_out = os.read(master, 4096).decode("utf-8", errors="ignore")
                    detected = extract_port_from_text(cmd_out)
            except Exception:
                pass
        if detected:
            try:
                Path(detected_port_file).write_text(str(detected), encoding="utf-8")
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
        srv.listen(5)
    except Exception:
        try:
            os.close(master)
        except OSError:
            pass
        return

    last_active = time.time()
    while True:
        try:
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
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                if not data:
                    conn.close()
                    continue
                try:
                    req = json.loads(data.decode("utf-8").strip())
                except Exception:
                    conn.close()
                    continue
                if req.get("cmd") == "exit":
                    conn.close()
                    break
                conn.sendall(json.dumps({"code": 0, "output": "ok"}).encode("utf-8") + b"\n")
                conn.close()
            if time.time() - last_active > idle_timeout:
                break
        except Exception:
            break

    try:
        os.write(master, b"exit\n")
        time.sleep(0.05)
        os.close(master)
    except Exception:
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
    detected_port_file: Optional[str] = None,
) -> None:
    """Daemonize and start the background interactive shell worker."""
    if os.path.exists(sock_path):
        try:
            os.unlink(sock_path)
        except OSError:
            pass

    pid = os.fork()
    if pid == 0:
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.close(devnull)
        d_pid = os.fork()
        if d_pid == 0:
            run_worker_server(
                sock_path=sock_path,
                master_ssh_sock=master_ssh_sock,
                user=user,
                host=host,
                port=port,
                default_node=default_node,
                detected_port_file=detected_port_file,
            )
        os._exit(0)
    os.waitpid(pid, 0)


def ensure_background_interactive_session(
    username: str,
    hpc_address: str,
    hpc: str,
    default_node: Optional[str] = None,
) -> Tuple[bool, Optional[int]]:
    """
    Step 2a: Launches a persistent background interactive session to the compute head node
    (e.g. gaea71, ufe04, gaea61) using the established ControlMaster socket.
    Reuses existing worker session if already running.
    Returns (success, detected_port).
    """
    node = default_node or DEFAULT_HEAD_NODES.get(hpc.lower())
    worker_sock = get_worker_sock_path(username, hpc)
    master_sock = str(Path.home() / ".ssh" / f"master-{username}@{hpc_address}:22")
    port_file = f"/tmp/hpc_detected_port_{username}_{hpc.lower()}.txt"

    if os.path.exists(port_file):
        try:
            os.unlink(port_file)
        except OSError:
            pass

    if is_socket_alive(worker_sock):
        detected = detect_remote_port(hpc, username, worker_sock)
        return True, detected

    spawn_worker(
        sock_path=worker_sock,
        master_ssh_sock=master_sock,
        user=username,
        host=hpc_address,
        default_node=node,
        detected_port_file=port_file,
    )

    # Poll for worker socket readiness (up to 55s to allow bastion & headnode login)
    t0 = time.time()
    while time.time() - t0 < 55:
        if is_socket_alive(worker_sock):
            detected = detect_remote_port(hpc, username, worker_sock)
            return True, detected
        time.sleep(0.3)

    print("TIMEOUT [!]")
    print(f"[!] Warning: Background interactive session did not become ready within 55s.", file=sys.stderr)
    return False, detect_remote_port(hpc, username)


def establish_master_connection(username: str, hpc_address: str, hpc: str) -> bool:
    """
    Step 1 of user plan:
    Non-interactive login to the original HPC address to establish the ControlMaster
    socket for multiplexing.
    IF THE CONTROL MASTER EXISTS, WE SKIP THIS STEP!
    """
    if is_master_alive(username, hpc_address):
        print(f"[✓] Step 1: ControlMaster session already active for {hpc_address}. Skipping login.")
        return True

    print("\n" + "=" * 70)
    print(f"[*] STEP 1: LOGGING IN TO HPC ADDRESS ({hpc.upper()})")
    print("=" * 70)
    print(f"Connecting to: {username}@{hpc_address}")
    print("[!] Push your USB key / YubiKey and enter your PIN/password when prompted.")
    print("[!] Establishes the SSH ControlMaster socket for background multiplexing.")
    print("-" * 70 + "\n")

    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-N", "-f",
        f"{username}@{hpc_address}",
    ]
    try:
        proc = subprocess.run(cmd)
        if proc.returncode != 0:
            print(f"[!] Step 1 login failed with code {proc.returncode}.", file=sys.stderr)
            return False
    except KeyboardInterrupt:
        print("\n[!] Aborted by user.")
        return False

    time.sleep(1.0)
    if is_master_alive(username, hpc_address):
        print(f"[✓]  Complete: ControlMaster connection established on {hpc_address}.")
        return True
    else:
        print(f"[!] Warning: ControlMaster socket not detected for {hpc_address}.", file=sys.stderr)
        return False


def setup_background_port_forward(
    username: str,
    hpc_address: str,
    my_local_port: int,
    remote_local_port: int,
    hpc: str,
    default_node: Optional[str] = None,
    skip_bg_session: bool = False,
) -> bool:
    """
    Step 2 of user plan:
    1. Launches a background interactive session to the head node (if not already running).
    2. Runs local port forwarding to the background using the ControlMaster connection.
    """
    if not skip_bg_session:
        print("\n" + "=" * 70)
        print(f"[*] STEP 2: CREATE PORT FORWARDING ({hpc.upper()})")
        print("=" * 70)
        ensure_background_interactive_session(username, hpc_address, hpc, default_node=default_node)

    # Part B: Run local port forwarding
    print(f"[*] Setting up port forward: localhost:{my_local_port} -> {hpc_address}:localhost:{remote_local_port}")
    cleanup_stale_port_forward(my_local_port)

    # 1. Try dynamic forward via ControlMaster (-O forward)
    cmd_fwd = [
        "ssh",
        "-O", "forward",
        f"-L{my_local_port}:localhost:{remote_local_port}",
        f"{username}@{hpc_address}",
    ]
    res = subprocess.run(cmd_fwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode == 0:
        time.sleep(0.5)
        print(f"[✓] Complete: Port forwarding active on localhost:{my_local_port}.")
        return True

    # 2. Fallback to multiplexed background session with BatchMode
    cmd_bg = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-N", "-f",
        f"-L{my_local_port}:localhost:{remote_local_port}",
        f"{username}@{hpc_address}",
    ]
    subprocess.run(cmd_bg)
    time.sleep(1.0)

    if is_local_port_in_use(my_local_port):
        print(f"[✓] Complete: Tunnel listening on localhost:{my_local_port}.")
        return True
    else:
        print(f"[!] Warning: Local port {my_local_port} is not listening.", file=sys.stderr)
        return False


def establish_tunnel(
    username: str,
    hpc_address: str,
    my_local_port: Optional[int],
    remote_local_port: Optional[int],
    hpc: str,
    config_path: Path,
) -> Tuple[bool, int, int]:
    """
    Coordinates Step 1 and Step 2 of user plan:
    1. Establish non-interactive ControlMaster session on original HPC address (skipped if already active).
    2. Launch background interactive session to head node, auto-detecting remote port if needed.
    3. Run local port forwarding over the master connection.
    Returns (success, my_local_port, remote_local_port).
    """
    # Step 1: Non-interactive login to HPC address to establish ControlMaster socket
    # (If the ControlMaster socket exists, we skip this step)
    if not establish_master_connection(username, hpc_address, hpc):
        return False, my_local_port or 0, remote_local_port or 0

    # Step 2: Create port forwarding
    print("\n" + "=" * 70)
    print(f"[*] STEP 2: CREATE PORT FORWARDING ({hpc.upper()})")
    print("=" * 70)

    bg_ok, detected_port = ensure_background_interactive_session(username, hpc_address, hpc)
    if not bg_ok:
        print(f"[!] Warning: Background interactive session to head node reported an issue.", file=sys.stderr)

    if not remote_local_port:
        remote_local_port = detected_port or detect_remote_port(hpc, username)
        if remote_local_port:
            print(f"[+] Auto-detected remote port: {remote_local_port}")

    # Fallback to prompt only if auto-detection was unable to find port
    if not remote_local_port:
        try:
            user_input = input(f"[?] Enter your assigned remote port for '{hpc}' (e.g. 40894 or 45894): ").strip()
            remote_local_port = int(user_input)
        except (ValueError, KeyboardInterrupt, EOFError):
            print("\n[!] Error: Valid remote port is required.", file=sys.stderr)
            return False, 0, 0

    if not my_local_port:
        my_local_port = compute_local_port(hpc, remote_local_port)

    # Save automatically detected/verified ports into .hpc_config
    update_hpc_config(
        path=config_path,
        hpc=hpc,
        username=username,
        my_local_port=my_local_port,
        remote_local_port=remote_local_port,
    )

    # Step 2b: Run local port forwarding over the master connection
    if not setup_background_port_forward(
        username=username,
        hpc_address=hpc_address,
        my_local_port=my_local_port,
        remote_local_port=remote_local_port,
        hpc=hpc,
        skip_bg_session=True,
    ):
        return False, my_local_port, remote_local_port

    return True, my_local_port, remote_local_port


def login_local_port(username: str, my_local_port: int, hpc: str) -> bool:
    """
    Step 3 of user plan:
    SSH to local port to connect to cluster, using non-interactive login ('true').
    Establishes and persists the local ControlMaster session.
    """
    print("\n" + "=" * 70)
    print(f"[*] STEP 3: CONNECTING THROUGH LOCAL PORT ({hpc.upper()})")
    print("=" * 70)
    print(f"Command: ssh -p {my_local_port} {username}@localhost true")
    print("[!] Enter your password/PIN if prompted (session will persist in background).")
    print("-" * 70 + "\n")

    cleanup_stale_master(username, "localhost", my_local_port)
    cleanup_local_known_hosts(my_local_port)

    cmd = [
        "ssh",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "StrictHostKeyChecking=no",
        "-o", "LogLevel=ERROR",
        "-p", str(my_local_port),
        f"{username}@localhost",
        "true",
    ]

    for attempt in range(2):
        try:
            proc = subprocess.run(cmd)
            if proc.returncode == 0:
                print(f"[✓] Complete: Connected through localhost:{my_local_port}.")
                return True
            if attempt == 0:
                time.sleep(1.5)
        except KeyboardInterrupt:
            print("\n[!] Aborted by user.")
            return False

    print(f"[!] Local login command exited with code {proc.returncode}.")
    return False


def ensure_tunnel(
    hpc: str,
    username: Optional[str] = None,
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Python API for AutoMATS: Ensures the tunnel for `hpc` is active.
    Returns dictionary with configuration and local port.
    """
    cfg_file = config_path or get_default_config_path()
    if not cfg_file.exists():
        create_template_hpc_config(cfg_file, username, target_cluster=hpc_key)

    cfg_all = sync_missing_ports_in_config(cfg_file, username, target_cluster=hpc_key)
    hpc_key = hpc.lower()
    entry = cfg_all.get(hpc_key, {})

    user = resolve_username(hpc_key, username, cfg_all)
    if not user or user == "<your_username>":
        raise ValueError(
            f"Username not configured in '{cfg_file.name}' for '{hpc}'. "
            f"Please configure username in '{cfg_file.name}' first."
        )

    if hpc_key not in cfg_all:
        det_rem = detect_remote_port(hpc_key, user)
        det_loc = compute_local_port(hpc_key, det_rem) if det_rem else None
        update_hpc_config(cfg_file, hpc_key, user, det_loc, det_rem)
        cfg_all = load_hpc_config(cfg_file)
        entry = cfg_all.get(hpc_key, {})

    ensure_ssh_config()

    remote_port = entry.get("remote_local_port")
    if not remote_port:
        remote_port = detect_remote_port(hpc_key, user)
    local_port = entry.get("my_local_port")
    if remote_port and not local_port:
        local_port = compute_local_port(hpc_key, remote_port)

    if local_port and test_local_access(user, local_port, timeout=1.0):
        return {
            "hpc": hpc_key,
            "username": user,
            "my_local_port": local_port,
            "remote_local_port": remote_port,
        }

    # Tunnel not active -> clean up any stale port binding and establish
    if local_port:
        cleanup_stale_port_forward(local_port)
    hpc_address = select_working_address(hpc_key)
    ok, final_local, final_remote = establish_tunnel(
        username=user,
        hpc_address=hpc_address,
        my_local_port=local_port,
        remote_local_port=remote_port,
        hpc=hpc_key,
        config_path=cfg_file,
    )
    if not ok:
        raise ConnectionError(f"Failed to establish tunnel for {hpc_key}.")

    login_local_port(user, final_local, hpc_key)

    if not test_local_access(user, final_local, timeout=3.0):
        raise ConnectionError(f"Failed to verify access through localhost:{final_local}.")

    return {
        "hpc": hpc_key,
        "username": user,
        "my_local_port": final_local,
        "remote_local_port": final_remote,
    }


# ==============================================================================
# CLI Entrypoint
# ==============================================================================

def print_usage(file=sys.stderr) -> None:
    print("Usage: ssh_helper.py [user_name] <gaeac6|gaeac7|ursa>", file=file)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ssh_helper.py",
        usage="ssh_helper.py [user_name] <gaeac6|gaeac7|ursa> [options]",
        description="Automated SSH Tunnel & Multiplexing Tool for AutoMATS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
        """,
    )

    parser.add_argument(
        "arg1",
        nargs="?",
        help="[user_name] or target cluster (gaeac6, gaeac7, ursa)",
    )
    parser.add_argument(
        "arg2",
        nargs="?",
        help="Target cluster if arg1 is user_name (gaeac6, gaeac7, ursa)",
    )
    parser.add_argument(
        "-u", "--user",
        dest="opt_user",
        help="RDHPCS username override",
    )
    parser.add_argument(
        "-c", "--hpc",
        dest="opt_hpc",
        choices=["gaeac6", "gaeac7", "ursa"],
        help="Target HPC cluster",
    )
    parser.add_argument(
        "-r", "--remote-port",
        type=int,
        help="Manually override assigned remote local port (optional)",
    )
    parser.add_argument(
        "-l", "--local-port",
        type=int,
        help="Manually override conflict-free local forwarding port (optional)",
    )
    parser.add_argument(
        "--config-file",
        type=Path,
        default=None,
        help="Path to .hpc_config file (default: searches CWD and AutoMATS root)",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    valid_clusters = list(HPC_ENDPOINTS.keys())
    username = args.opt_user
    hpc = args.opt_hpc

    pos_args = [a for a in [args.arg1, args.arg2] if a]

    if len(pos_args) == 1:
        val = pos_args[0].lower()
        if val in valid_clusters:
            hpc = val
        else:
            username = pos_args[0]
    elif len(pos_args) >= 2:
        val1 = pos_args[0].lower()
        val2 = pos_args[1].lower()
        if val2 in valid_clusters:
            username = pos_args[0]
            hpc = val2
        elif val1 in valid_clusters:
            hpc = val1
            username = pos_args[1]
        else:
            print(f"Error: Invalid HPC cluster. Must be one of: {', '.join(valid_clusters)}", file=sys.stderr)
            print_usage()
            return 1

    config_path = args.config_file or get_default_config_path()

    # If .hpc_config does not exist, create template only for requested cluster
    if not config_path.exists():
        create_template_hpc_config(config_path, username, target_cluster=hpc)
        if not hpc:
            print_usage()
            return 1

    if not hpc:
        print_usage()
        return 1

    existing_all = sync_missing_ports_in_config(config_path, username, target_cluster=hpc)
    existing_cfg = existing_all.get(hpc, {})

    username = resolve_username(hpc, username, existing_all)

    if not username or username == "<your_username>":
        print(f"[!] Error: 'username' could not be resolved for '{hpc}'.", file=sys.stderr)
        print(f"Please specify your username as an argument or in {config_path.name}.", file=sys.stderr)
        return 1

    # If this cluster is not yet in .hpc_config, initialize it incrementally with auto-detected ports!
    if hpc not in existing_all:
        det_rem = detect_remote_port(hpc, username)
        det_loc = compute_local_port(hpc, det_rem) if det_rem else None
        update_hpc_config(config_path, hpc, username, det_loc, det_rem)
        existing_all = load_hpc_config(config_path)
        existing_cfg = existing_all.get(hpc, {})

    # Step 1: Ensure ~/.ssh/config contains ControlMaster configuration
    ensure_ssh_config()

    # Step 2: Handle known ports from .hpc_config or CLI override
    remote_local_port: Optional[int] = args.remote_port or existing_cfg.get("remote_local_port")
    my_local_port: Optional[int] = args.local_port or existing_cfg.get("my_local_port")
    if not remote_local_port:
        remote_local_port = detect_remote_port(hpc, username)
        if remote_local_port:
            print(f"[*] Auto-detected remote port from configuration: {remote_local_port}")

    if remote_local_port and my_local_port is None:
        my_local_port = compute_local_port(hpc, remote_local_port)

    # Step 3: Check if local port access is ALREADY established and usable right now!
    if my_local_port:
        print("[*] Checking local port connection...", end=" ", flush=True)
        if test_local_access(username, my_local_port, timeout=2.0):
            print("ACTIVE [✓]")
            print(f"[✓] SSH tunnel for {hpc} is active on localhost:{my_local_port}.")
            return 0
        else:
            print("INACTIVE / NOT READY")
            cleanup_stale_port_forward(my_local_port)

    # Step 4: Select working HPC address with priority fallback
    try:
        hpc_address = select_working_address(hpc)
    except ConnectionError as err:
        print(f"\n[!] Connection Error: {err}", file=sys.stderr)
        return 1

    # Display port configuration if known
    print(f"\n[*] Port Configuration for {hpc.upper()}:")
    print(f"    - My Local Port:     {my_local_port if my_local_port else '[auto-detect]'}")
    print(f"    - Remote Local Port: {remote_local_port if remote_local_port else '[auto-detect]'}")

    # Step 1 & 2: Establish master connection & launch background port forwarding
    tunnel_ok, final_local, final_remote = establish_tunnel(
        username=username,
        hpc_address=hpc_address,
        my_local_port=my_local_port,
        remote_local_port=remote_local_port,
        hpc=hpc,
        config_path=config_path,
    )

    if not tunnel_ok:
        print("\n[!] Failed to establish tunnel to HPC address.", file=sys.stderr)
        return 1

    # Step 3: Non-interactive login through local port to establish local ControlMaster
    login_local_port(
        username=username,
        my_local_port=final_local,
        hpc=hpc,
    )

    # Verification: Test whether we may actually access through the local port
    print(f"[*] Verifying access through localhost:{final_local} ...", end=" ", flush=True)
    if test_local_access(username, final_local, timeout=3.0):
        print("ACTIVE [✓]")
        print("[✓]  All good!")
        return 0
    else:
        print("FAILED [!]")
        print(f"\n[!] Unable to verify access through localhost:{final_local}.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
