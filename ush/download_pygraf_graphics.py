#!/usr/bin/env python3
"""
download_pygraf_graphics.py: Standalone CLI utility to download pygraf graphics from HPC.

Usage:
    ush/download_pygraf_graphics.py <recipe.yaml> [options]

Options:
    -o, --output-dir DIR   Local destination directory (default: ./graphics)
    --update               Re-download existing files instead of using local cache
    --dry-run              List all targeted remote files without downloading
    --debug                Print verbose transfer details, including missing or uncopied files
    -h, --help             Show this help message and exit
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# Auto-re-exec with local venv python if available and not already active
ROOT = Path(__file__).resolve().parent.parent
venv_dir = ROOT / ".venv"
venv_python = venv_dir / "bin" / "python"
if __name__ == "__main__" and venv_python.is_file() and Path(sys.prefix).resolve() != venv_dir.resolve():
    try:
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve())] + sys.argv[1:])
    except (PermissionError, OSError):
        pass


def _resolve_ssh_helper_path() -> Path:
    """Returns path to ush/ssh_helper.py."""
    return Path(__file__).resolve().parent / "ssh_helper.py"


def find_hpc_config_file() -> Optional[Path]:
    """Finds .hpc_config path in CWD or AutoMATS repository root."""
    cwd_dot = Path.cwd() / ".hpc_config"
    if cwd_dot.is_file():
        return cwd_dot
    root_dot = ROOT / ".hpc_config"
    if root_dot.is_file():
        return root_dot
    return None


def _clean_hpc_username(value: Any) -> Optional[str]:
    """Normalizes a configured username string, returning None if unset or placeholder."""
    if value is None:
        return None
    val = str(value).strip().strip("'\"")
    if not val or val.lower() in ("null", "none", "~", "<your_username>"):
        return None
    return val


def load_hpc_users(config_path: Optional[Path] = None) -> Dict[str, str]:
    """
    Loads simple `<cluster>: <username>` mappings from `.hpc_config`.
    Also tolerates legacy nested `<cluster>:\\n  username: <username>` blocks.
    """
    path = config_path or find_hpc_config_file()
    if not path or not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    users: Dict[str, str] = {}
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
            user = _clean_hpc_username(v)
            if v.strip():
                current_section = None
                if user:
                    users[key] = user
            else:
                current_section = key
        elif current_section and key in ("username", "user"):
            user = _clean_hpc_username(v)
            if user:
                users[current_section] = user

    return users


def _is_unix_socket_alive(socket_path: str) -> bool:
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


def _ping_worker_socket(
    socket_path: str,
    timeout: float = 4.5,
    probe_remote: bool = True,
) -> bool:
    """Verifies that a worker daemon socket responds to ping (and remote probe)."""
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


def test_hpc_session_ready(cluster: str, username: str = "") -> bool:
    """Verifies that the persistent background HPC terminal session is active for `cluster`."""
    import glob

    hpc_key = cluster.strip().lower()
    if not hpc_key:
        return False
    if hpc_key == "localhost":
        return True

    candidates: List[str] = []
    if username and username != "<your_username>":
        candidates.append(f"/tmp/hpc_term_{username}_{hpc_key}.sock")
    candidates.extend(sorted(glob.glob(f"/tmp/hpc_term_*_{hpc_key}.sock")))

    seen: set[str] = set()
    for sock_path in candidates:
        if sock_path in seen:
            continue
        seen.add(sock_path)
        if _ping_worker_socket(sock_path, timeout=3.0, probe_remote=True):
            return True
    return False


class _SshCheckStatusBar:
    """Single-line animated status bar on TTY (or periodic heartbeat in logs) while checking SSH."""

    def __init__(
        self,
        message: str,
        stream=None,
        interval: float = 0.15,
        heartbeat_interval: float = 5.0,
        enabled: bool = True,
        clock=time.monotonic,
    ) -> None:
        self.message = str(message).replace("\n", " ")
        self.stream = sys.stdout if stream is None else stream
        self.interval = max(0.02, float(interval))
        self.heartbeat_interval = max(1.0, float(heartbeat_interval))
        self.enabled = bool(enabled)
        self.clock = clock
        self.interactive = bool(getattr(self.stream, "isatty", lambda: False)())
        self.started = clock()
        self.frame = 0
        self.width = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _clear_locked(self) -> None:
        if self.interactive and self.width:
            self.stream.write("\r" + (" " * self.width) + "\r")
            self.stream.flush()
            self.width = 0

    def _draw_locked(self) -> None:
        elapsed = max(0, int(self.clock() - self.started))
        if self.interactive:
            track = 16
            slider = 4
            span = track - slider
            pos = self.frame % (2 * span)
            if pos > span:
                pos = (2 * span) - pos
            bar = ("." * pos) + ("=" * slider) + ("." * (span - pos))
            spinner = "|/-\\"[self.frame % 4]
            text = f"{spinner} [{bar}] {elapsed}s elapsed | {self.message}"
            columns = max(20, shutil.get_terminal_size(fallback=(100, 24)).columns - 1)
            text = text[:columns]
            self._clear_locked()
            self.stream.write("\r" + text)
            self.stream.flush()
            self.width = len(text)
            self.frame += 1
        else:
            self.stream.write(f"    ... {self.message} ({elapsed}s elapsed)\n")
            self.stream.flush()

    def _animate(self) -> None:
        wait_step = self.interval if self.interactive else self.heartbeat_interval
        while not self._stop.wait(wait_step):
            with self._lock:
                self._draw_locked()

    def __enter__(self):
        if self.enabled:
            self._thread = threading.Thread(target=self._animate, name="ssh-status-bar", daemon=True)
            self._thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        with self._lock:
            self._clear_locked()


def _ensure_hpc_worker_if_master_alive(
    cluster: str,
    username: str = "",
    check_session_first: bool = True,
    show_status: bool = True,
) -> bool:
    """
    Attempts non-interactive worker recovery via `ssh_helper.py --cmd true` so a
    wedged or stopped worker is replaced automatically whenever an active SSH
    ControlMaster already exists (and stale ControlMasters are closed).
    """
    if check_session_first and test_hpc_session_ready(cluster, username):
        return True
    hpc_key = cluster.strip().lower()
    if not hpc_key:
        return False
    if hpc_key == "localhost":
        return True
    ssh_helper_path = _resolve_ssh_helper_path()
    if not ssh_helper_path.is_file():
        return False
    cmd = [sys.executable, str(ssh_helper_path), hpc_key]
    if username and username != "<your_username>":
        cmd.extend(["-u", username])
    cmd.extend(["--cmd", "true"])
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        with _SshCheckStatusBar(f"Checking '{hpc_key}' SSH connection...", enabled=show_status):
            _, err_text = proc.communicate(timeout=48.0)
        if proc.returncode == 0:
            if show_status:
                print(
                    f"[✓] Background HPC terminal session for '{hpc_key}' is ready.",
                    flush=True,
                )
            return True
        if err_text:
            for raw_line in err_text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                low = line.lower()
                if any(
                    tok in low
                    for tok in ("stale", "failed to start", "timed out", "stalled", "authentication", "closed stale")
                ):
                    print(line, file=sys.stderr, flush=True)
        return False
    except subprocess.TimeoutExpired:
        if proc is not None:
            try:
                proc.terminate()
                proc.communicate(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                    proc.communicate(timeout=1.0)
                except Exception:
                    pass
        print(
            f"[!] Timed out verifying/recovering HPC terminal session for '{hpc_key}'.",
            file=sys.stderr,
            flush=True,
        )
        return False
    except (subprocess.SubprocessError, OSError):
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                pass
            if proc.stderr is not None:
                try:
                    proc.stderr.close()
                except OSError:
                    pass
        return False
    except BaseException:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
            if proc.stderr is not None:
                try:
                    proc.stderr.close()
                except OSError:
                    pass
        raise


def _parse_yaml_scalar(v: str) -> Any:
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [x.strip().strip("'\"") for x in inner.split(",") if x.strip()] if inner else []
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    if v.isdigit():
        return int(v)
    return v


def _split_yaml_kv(text: str) -> Optional[Tuple[str, str]]:
    """Splits a YAML `key: value` or `key:` pair without splitting on `cluster:/path` or URLs."""
    m = re.match(r"^([^:\[\]]+?):(?:\s+(.*)|\s*)$", text)
    if not m:
        return None
    return m.group(1).strip().strip("'\""), (m.group(2) or "").strip()


def parse_simple_yaml(text: str) -> Dict[str, Any]:
    """Pure-Python indentation-aware YAML parser for recipes without requiring PyYAML."""
    raw_lines: List[Tuple[int, str]] = []
    for raw in text.splitlines():
        line = raw
        if "#" in line:
            line = line.split("#", 1)[0]
        if line.strip():
            indent = len(line) - len(line.lstrip(" "))
            raw_lines.append((indent, line.strip()))

    idx = 0

    def parse_node(base_indent: int) -> Any:
        nonlocal idx
        if idx >= len(raw_lines):
            return None
        indent, line = raw_lines[idx]
        if line.startswith("- "):
            res_list: List[Any] = []
            list_indent = indent
            while idx < len(raw_lines):
                cur_indent, cur_line = raw_lines[idx]
                if cur_indent < list_indent:
                    break
                if cur_line.startswith("- "):
                    content = cur_line[2:].strip()
                    idx += 1
                    kv = _split_yaml_kv(content)
                    if kv is not None:
                        k, v = kv
                        item_dict: Dict[str, Any] = {}
                        if v:
                            item_dict[k] = _parse_yaml_scalar(v)
                        else:
                            if idx < len(raw_lines) and raw_lines[idx][0] > cur_indent:
                                item_dict[k] = parse_node(raw_lines[idx][0])
                            else:
                                item_dict[k] = None
                        while idx < len(raw_lines):
                            ni, nl = raw_lines[idx]
                            if ni <= cur_indent or nl.startswith("- "):
                                break
                            nkv = _split_yaml_kv(nl)
                            if nkv is not None:
                                nk, nv = nkv
                                idx += 1
                                if nv:
                                    item_dict[nk] = _parse_yaml_scalar(nv)
                                else:
                                    if idx < len(raw_lines) and raw_lines[idx][0] > ni:
                                        item_dict[nk] = parse_node(raw_lines[idx][0])
                                    else:
                                        item_dict[nk] = None
                            else:
                                idx += 1
                        res_list.append(item_dict)
                    else:
                        val = _parse_yaml_scalar(content)
                        if idx < len(raw_lines) and raw_lines[idx][0] > cur_indent and not raw_lines[idx][1].startswith("- "):
                            sub_block = parse_node(raw_lines[idx][0])
                            if isinstance(val, str) and isinstance(sub_block, dict):
                                res_list.append({val: sub_block})
                            else:
                                res_list.append(val)
                        else:
                            res_list.append(val)
                else:
                    break
            return res_list
        else:
            res_dict: Dict[str, Any] = {}
            dict_indent = indent
            while idx < len(raw_lines):
                cur_indent, cur_line = raw_lines[idx]
                if cur_indent < dict_indent:
                    break
                if cur_line.startswith("- "):
                    break
                kv = _split_yaml_kv(cur_line)
                if kv is not None:
                    k, v = kv
                    idx += 1
                    if v:
                        res_dict[k] = _parse_yaml_scalar(v)
                    else:
                        if idx < len(raw_lines) and (
                            raw_lines[idx][0] > cur_indent
                            or (raw_lines[idx][1].startswith("- ") and raw_lines[idx][0] >= cur_indent)
                        ):
                            res_dict[k] = parse_node(raw_lines[idx][0])
                        else:
                            res_dict[k] = None
                else:
                    idx += 1
            return res_dict

    parsed = parse_node(0)
    return parsed if isinstance(parsed, dict) else {}


def load_recipe_yaml(path: Path) -> Dict[str, Any]:
    """Loads recipe YAML file using PyYAML or our pure-Python fallback parser."""
    try:
        import yaml
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
            if isinstance(data, dict):
                return data
    except ImportError:
        pass

    raw = path.read_text(encoding="utf-8")
    return parse_simple_yaml(raw)


def extract_expname(remote_path: str) -> str:
    """
    Extracts expname from the string before '/com' and after the previous '/'.
    E.g. /gpfs/f6/.../OPSROOT/RRFSv2X.retv4/com/rrfs/v2.1.4 -> RRFSv2X.retv4
    """
    clean = remote_path.rstrip("/")
    m = re.search(r"/([^/]+)/com(?:/|$)", clean)
    if m:
        return m.group(1)
    parts = clean.split("/")
    if "com" in parts:
        idx = parts.index("com")
        if idx > 0:
            return parts[idx - 1]
    return parts[-1]


def detect_prefix_from_path(comroot: str) -> str:
    """
    Extracts the model prefix from a COMROOT path.
    E.g. /gpfs/f6/.../com/rrfs/v2.1.4 -> rrfs
         /com/hrrr/v4.1 -> hrrr
         /scratch1/.../com/rap/v5.0 -> rap
    """
    clean = comroot.rstrip("/")
    m = re.search(r"/com/([^/]+)(?:/|$)", clean)
    if m:
        return m.group(1)
    parts = clean.split("/")
    if "com" in parts:
        idx = parts.index("com")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return ""


def detect_prefix(remote_comroot: str, username: str = "", cluster: str = "") -> str:
    """
    Detects model prefix under COMROOT.
    1. First tries extracting from the path directly: .../com/<prefix>/... -> <prefix>
    2. If not found in path and cluster session is active, queries remote COMROOT for <prefix>.YYYYMMDD directories.
    3. Falls back to 'rrfs' as default.
    """
    p = detect_prefix_from_path(remote_comroot)
    if p:
        return p

    if cluster and test_hpc_session_ready(cluster, username):
        try:
            ssh_helper_path = _resolve_ssh_helper_path()
            cmd = [sys.executable, str(ssh_helper_path), cluster.lower()]
            if username:
                cmd.extend(["-u", username])
            cmd.extend([
                "--cmd",
                f"ls -d {remote_comroot.rstrip('/')}/*.[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9] 2>/dev/null | head -n1",
            ])
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10.0)
            if res.returncode == 0 and res.stdout.strip():
                dir_name = Path(res.stdout.strip()).name
                if "." in dir_name:
                    cand = dir_name.split(".")[0].strip()
                    if cand:
                        return cand
        except Exception:
            pass

    return "rrfs"


def parse_fhr_list(raw_fhr: Any) -> List[int]:
    """
    Parses fhr parameter into list of integer forecast lead hours.
    Accepts: [0-6], [0, 1, 2], [0-12-3], etc.
    """
    items = raw_fhr if isinstance(raw_fhr, list) else [raw_fhr]
    fhrs = []
    for item in items:
        if isinstance(item, int):
            if item < 0:
                # Defensive handling for unquoted 0-72 parsed as -72
                fhrs.extend(range(0, abs(item) + 1))
            else:
                fhrs.append(item)
        elif isinstance(item, str):
            s = item.strip().strip("'\"")
            # Range format: start-end or start-end-step
            m = re.match(r"^(\d+)-(\d+)(?:-(\d+))?$", s)
            if m:
                start = int(m.group(1))
                end = int(m.group(2))
                step = int(m.group(3)) if m.group(3) else 1
                fhrs.extend(range(start, end + 1, step))
            elif s.isdigit():
                fhrs.append(int(s))
            else:
                raise ValueError(f"Invalid fhr specification: {item}")
        else:
            raise ValueError(f"Invalid fhr type: {type(item)}")
    return sorted(list(set(fhrs)))


def parse_fhr_config(raw_fhr: Any) -> Dict[str, Any]:
    """
    Parses fhr parameter into structured configuration mapping cycle hours to forecast lead hours.
    Supports:
      1) Legacy flat format: [0-6], 0-6, [0, 1, 2]
      2) List of dicts / mapping:
         fhr:
           - default: [0, 1]
           - '00,12': [0-72]
      3) Direct dict:
         fhr:
           default: [0, 1]
           '00,12': [0-72]
    """
    cfg: Dict[str, Any] = {
        "specific": [],  # list of (set_of_hours: set[int], fhrs: list[int])
        "default": None,
        "legacy": None,
    }

    def _parse_val(val: Any) -> List[int]:
        if isinstance(val, str):
            v_str = val.strip()
            if v_str.startswith("[") and v_str.endswith("]"):
                inner = v_str[1:-1].strip()
                val = [x.strip() for x in inner.split(",") if x.strip()] if inner else []
        return parse_fhr_list(val)

    if isinstance(raw_fhr, dict):
        for k, v in raw_fhr.items():
            k_str = str(k).strip().strip("'\"").lower()
            if k_str == "default":
                cfg["default"] = _parse_val(v)
            else:
                hours = set()
                for h in k_str.split(","):
                    h = h.strip()
                    if h.isdigit():
                        hours.add(int(h))
                if hours:
                    cfg["specific"].append((hours, _parse_val(v)))
        return cfg

    items = raw_fhr if isinstance(raw_fhr, list) else [raw_fhr]

    is_mapping = False
    for item in items:
        if isinstance(item, dict):
            is_mapping = True
            break
        if isinstance(item, str) and ":" in item:
            is_mapping = True
            break

    if not is_mapping:
        cfg["legacy"] = parse_fhr_list(raw_fhr)
        return cfg

    for item in items:
        if isinstance(item, dict):
            for k, v in item.items():
                k_str = str(k).strip().strip("'\"").lower()
                if k_str == "default":
                    cfg["default"] = _parse_val(v)
                else:
                    hours = set()
                    for h in k_str.split(","):
                        h = h.strip()
                        if h.isdigit():
                            hours.add(int(h))
                    if hours:
                        cfg["specific"].append((hours, _parse_val(v)))
        elif isinstance(item, str) and ":" in item:
            k, v = item.split(":", 1)
            k_str = k.strip().strip("'\"").lower()
            if k_str == "default":
                cfg["default"] = _parse_val(v)
            else:
                hours = set()
                for h in k_str.split(","):
                    h = h.strip()
                    if h.isdigit():
                        hours.add(int(h))
                if hours:
                    cfg["specific"].append((hours, _parse_val(v)))

    return cfg


def resolve_fhrs_for_cycle(fhr_cfg: Dict[str, Any], cyc_str: str) -> List[int]:
    """
    Returns the sorted list of forecast lead hours for the specified cycle string (YYYYMMDDHH).
    """
    if fhr_cfg.get("legacy") is not None:
        return fhr_cfg["legacy"]

    try:
        cyc_hour = int(cyc_str[8:10])
    except (IndexError, ValueError):
        cyc_hour = -1

    for hours_set, fhrs in fhr_cfg.get("specific", []):
        if cyc_hour in hours_set:
            return fhrs

    if fhr_cfg.get("default") is not None:
        return fhr_cfg["default"]

    return [0]


def parse_cycle_list(raw_cycle: Any) -> List[str]:
    """
    Parses cycle parameter into list of 10-digit YYYYMMDDHH strings.
    Accepts: [2024050600-2024051300], [2024050600-2024051300-24], [2024050600, 2024050700].
    If step is missing, defaults to 1h.
    """
    items = raw_cycle if isinstance(raw_cycle, list) else [raw_cycle]
    cycles = []
    for item in items:
        s = str(item).strip()
        # Range format: YYYYMMDDHH-YYYYMMDDHH or YYYYMMDDHH-YYYYMMDDHH-step
        m = re.match(r"^(\d{10})-(\d{10})(?:-(\d+))?$", s)
        if m:
            s_str = m.group(1)
            e_str = m.group(2)
            step_hours = int(m.group(3)) if m.group(3) else 1
            dt_start = datetime.strptime(s_str, "%Y%m%d%H")
            dt_end = datetime.strptime(e_str, "%Y%m%d%H")
            curr = dt_start
            while curr <= dt_end:
                cycles.append(curr.strftime("%Y%m%d%H"))
                curr += timedelta(hours=step_hours)
        elif re.match(r"^\d{10}$", s):
            cycles.append(s)
        else:
            raise ValueError(f"Invalid cycle specification: {item}")

    # Deduplicate while preserving order
    seen = set()
    res = []
    for c in cycles:
        if c not in seen:
            seen.add(c)
            res.append(c)
    return res


def cleanup_empty_directories(root_dir: Path) -> int:
    """
    Recursively removes empty directories under root_dir bottom-up.
    Also removes root_dir itself if it contains no files.
    Returns the count of removed empty directories.
    """
    removed_count = 0
    if not root_dir.exists() or not root_dir.is_dir():
        return 0

    for dirpath, dirnames, filenames in os.walk(str(root_dir), topdown=False):
        d = Path(dirpath)
        try:
            # Check items in directory (ignoring .DS_Store)
            items = [item for item in d.iterdir() if item.name != ".DS_Store"]
            if not items:
                # Remove leftover .DS_Store files first if any
                for item in d.iterdir():
                    try:
                        item.unlink(missing_ok=True)
                    except OSError:
                        pass
                d.rmdir()
                removed_count += 1
        except OSError:
            pass

    return removed_count


def stream_tar_pull(
    username: str,
    tasks: List[Tuple[str, Path]],
    cluster: str = "HPC",
    debug: bool = False,
) -> Tuple[int, int, int]:
    """
    Downloads remote files using a single streaming tar session over SSH.
    - Sends unique remote file paths to remote `tar -P -cf - --null -T -` via stdin.
    - Reads the incoming byte stream locally via `tarfile.open(mode='r|*')`.
    - Directly writes each member file to all requested local destination paths.
    - Missing files are ignored by remote tar and recorded as missing locally.
    Returns (completed_count, missing_count, error_count).
    """
    if not tasks:
        return 0, 0, 0

    dest_map: Dict[str, List[Path]] = {}
    unique_remote_paths: List[str] = []
    seen_dest_keys: set = set()
    task_counts: Dict[Tuple[str, str], int] = {}

    for r, l in tasks:
        norm = r.lstrip("/")
        d_key = (norm, os.path.abspath(str(l)))
        task_counts[d_key] = task_counts.get(d_key, 0) + 1
        if norm not in dest_map:
            dest_map[norm] = []
            unique_remote_paths.append(r)
        if d_key not in seen_dest_keys:
            seen_dest_keys.add(d_key)
            dest_map[norm].append(l)

    received_keys: set = set()
    error_keys: set = set()
    total_tasks = len(tasks)

    # Pack unique remote paths separated by null byte \0 for tar --null -T -
    path_data = ("\0".join(unique_remote_paths) + "\0").encode("utf-8")

    ssh_helper_path = _resolve_ssh_helper_path()
    cmd = [sys.executable, str(ssh_helper_path), cluster.lower()]
    if username:
        cmd.extend(["-u", username])
    cmd.extend(["--cmd", "tar -P -cf - --null -T -"])

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Feeder thread: writes paths to stdin and closes it
    def feed_stdin():
        try:
            if proc.stdin:
                proc.stdin.write(path_data)
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    t_in = threading.Thread(target=feed_stdin, daemon=True)
    t_in.start()

    # Stderr reader thread: drains stderr to prevent pipe buffer deadlock
    stderr_lines: List[str] = []

    def drain_stderr():
        try:
            if proc.stderr:
                for line in iter(proc.stderr.readline, b""):
                    clean = line.decode("utf-8", errors="ignore").strip()
                    if clean:
                        stderr_lines.append(clean)
        except Exception:
            pass

    t_err = threading.Thread(target=drain_stderr, daemon=True)
    t_err.start()

    completed = 0
    errors = 0
    aborted = False
    tar_read_error: Optional[tarfile.ReadError] = None
    stream_os_error: Optional[Exception] = None
    proc_rc: Optional[int] = None

    try:
        if proc.stdout:
            with tarfile.open(fileobj=proc.stdout, mode="r|*") as tar_stream:
                for member in tar_stream:
                    if not member.isfile():
                        continue
                    norm = member.name.lstrip("/")
                    dests = [
                        d for d in dest_map.get(norm, [])
                        if (norm, os.path.abspath(str(d))) not in received_keys
                    ]
                    if not dests:
                        continue

                    prev_done = completed + errors

                    try:
                        fin = tar_stream.extractfile(member)
                    except Exception as e:
                        for dest in dests:
                            d_key = (norm, os.path.abspath(str(dest)))
                            if d_key not in error_keys and d_key not in received_keys:
                                error_keys.add(d_key)
                                errors += task_counts.get(d_key, 1)
                            if debug:
                                print(f"  [DEBUG] Error extracting {dest}: {e}", file=sys.stderr)
                        fin = None

                    if fin:
                        primary_dest: Optional[Path] = None
                        primary_idx = -1
                        stream_err: Optional[Exception] = None

                        for idx, dest in enumerate(dests):
                            d_key = (norm, os.path.abspath(str(dest)))
                            try:
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                fout = dest.open("wb")
                            except Exception as e:
                                if d_key not in error_keys and d_key not in received_keys:
                                    error_keys.add(d_key)
                                    errors += task_counts.get(d_key, 1)
                                if debug:
                                    print(f"  [DEBUG] Error writing {dest}: {e}", file=sys.stderr)
                                continue

                            try:
                                with fout:
                                    shutil.copyfileobj(fin, fout, length=64 * 1024)
                            except Exception as e:
                                stream_err = e
                                primary_idx = idx
                                if d_key not in error_keys and d_key not in received_keys:
                                    error_keys.add(d_key)
                                    errors += task_counts.get(d_key, 1)
                                if debug:
                                    print(f"  [DEBUG] Error writing {dest}: {e}", file=sys.stderr)
                                try:
                                    dest.unlink(missing_ok=True)
                                except OSError:
                                    pass
                                break

                            primary_dest = dest
                            primary_idx = idx
                            break

                        if stream_err is not None:
                            for dest in dests[primary_idx + 1:]:
                                d_key = (norm, os.path.abspath(str(dest)))
                                if d_key not in error_keys and d_key not in received_keys:
                                    error_keys.add(d_key)
                                    errors += task_counts.get(d_key, 1)
                                if debug:
                                    print(
                                        f"  [DEBUG] Error writing {dest}: aborted after stream failure ({stream_err})",
                                        file=sys.stderr,
                                    )
                        elif primary_dest is not None:
                            if primary_dest.exists() and primary_dest.stat().st_size > 0:
                                p_key = (norm, os.path.abspath(str(primary_dest)))
                                if p_key not in received_keys:
                                    received_keys.add(p_key)
                                    completed += task_counts.get(p_key, 1)

                                for dest in dests[primary_idx + 1:]:
                                    d_key = (norm, os.path.abspath(str(dest)))
                                    try:
                                        dest.parent.mkdir(parents=True, exist_ok=True)
                                        shutil.copy2(primary_dest, dest)
                                        if dest.exists() and dest.stat().st_size > 0:
                                            if d_key not in received_keys:
                                                received_keys.add(d_key)
                                                completed += task_counts.get(d_key, 1)
                                        elif dest.exists() and dest.stat().st_size == 0:
                                            dest.unlink(missing_ok=True)
                                    except Exception as e:
                                        if d_key not in error_keys and d_key not in received_keys:
                                            error_keys.add(d_key)
                                            errors += task_counts.get(d_key, 1)
                                        if debug:
                                            print(f"  [DEBUG] Error writing {dest}: {e}", file=sys.stderr)
                            elif primary_dest.exists() and primary_dest.stat().st_size == 0:
                                primary_dest.unlink(missing_ok=True)

                    total_done = completed + errors
                    if total_done > prev_done and (total_done // 10 > prev_done // 10 or total_done == total_tasks):
                        pct = int(100 * total_done / total_tasks)
                        print(f"  Progress: {total_done}/{total_tasks} ({pct}%) [{completed} ok]", flush=True)

    except tarfile.ReadError as e:
        tar_read_error = e
    except (subprocess.SubprocessError, OSError) as e:
        stream_os_error = e
        if completed == 0:
            errors += 1
            print(f"[!] Error reading tar stream: {e}", file=sys.stderr)
    except BaseException:
        aborted = True
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=0.5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        raise
    finally:
        if aborted:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception:
                        pass
            try:
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
            t_in.join(timeout=0.5)
            t_err.join(timeout=0.5)
        else:
            t_in.join(timeout=3.0)
            t_err.join(timeout=3.0)
            try:
                proc_rc = proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc_rc = proc.wait()
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception:
                        pass

    unexpected_stderr = [
        line for line in stderr_lines
        if "Cannot stat: No such file or directory" not in line
        and "Error exit delayed" not in line
        and "Exiting with failure status" not in line
        and "Removing leading" not in line
    ]
    has_stat_missing = any(
        "Cannot stat: No such file or directory" in line for line in stderr_lines
    )

    if unexpected_stderr:
        header = "[DEBUG] Remote stderr output:" if debug else "[!] Remote / SSH helper stderr:"
        print(f"\n{header}", file=sys.stderr)
        for u in unexpected_stderr[:10]:
            print(f"  {u}", file=sys.stderr)

    if tar_read_error is not None:
        benign_empty = (
            completed == 0
            and not unexpected_stderr
            and stream_os_error is None
            and (proc_rc == 0 or has_stat_missing)
        )
        if not benign_empty:
            errors += 1
            print(f"[!] Error reading tar stream: {tar_read_error}", file=sys.stderr)
    elif (
        stream_os_error is None
        and unexpected_stderr
        and isinstance(proc_rc, int)
        and proc_rc != 0
        and errors == 0
    ):
        errors += 1

    missing_tasks = [
        (r, l) for r, l in tasks
        if (r.lstrip("/"), os.path.abspath(str(l))) not in received_keys
        and (r.lstrip("/"), os.path.abspath(str(l))) not in error_keys
    ]
    missing = len(missing_tasks)

    # In debug mode, list missing files
    if debug and missing > 0:
        for r_path, _ in missing_tasks:
            print(f"  [DEBUG] Missing on {cluster}: {Path(r_path).name}\n          Remote path: {r_path}", file=sys.stderr)

    return completed, missing, errors


def normalize_comroots(raw_comroot: Any) -> List[str]:
    """
    Normalizes COMROOT from a single string or a list of strings,
    preserving order and removing duplicates/empty entries.
    """
    if raw_comroot is None:
        return []

    if isinstance(raw_comroot, str):
        items = [raw_comroot]
    elif isinstance(raw_comroot, (list, tuple)):
        items = list(raw_comroot)
    else:
        raise ValueError(f"COMROOT must be a string or list of strings, got {type(raw_comroot).__name__}")

    result: List[str] = []
    seen: set[str] = set()
    for item in items:
        if item is None:
            continue
        val = str(item).strip()
        if not val or val.lower() in ("null", "none", "~"):
            continue
        if val not in seen:
            seen.add(val)
            result.append(val)
    return result


def _compile_template_specs(recipe: Dict[str, Any]) -> List[Tuple[str, List[str], Dict[str, Any], List[str], List[str]]]:
    """Parses default cycle/fhr/WGF/tile settings and `graphics list` overrides from `recipe`."""
    raw_wgf = recipe.get("WGF", ["det"])
    default_wgf_list = raw_wgf if isinstance(raw_wgf, list) else [raw_wgf]
    default_wgf_list = [str(x).strip() for x in default_wgf_list if str(x).strip()]

    raw_tile = recipe.get("tile", ["full"])
    default_tile_list = raw_tile if isinstance(raw_tile, list) else [raw_tile]
    default_tile_list = [str(x).strip() for x in default_tile_list if str(x).strip()]

    default_cycle_raw = recipe.get("cycle")
    if default_cycle_raw is None:
        def_cf = recipe.get("cycle and fhr") or recipe.get("cycle_and_fhr") or {}
        if isinstance(def_cf, dict):
            default_cycle_raw = def_cf.get("cycle")

    default_fhr_raw = recipe.get("fhr")
    if default_fhr_raw is None:
        def_cf = recipe.get("cycle and fhr") or recipe.get("cycle_and_fhr") or {}
        if isinstance(def_cf, dict):
            default_fhr_raw = def_cf.get("fhr")
    if default_fhr_raw is None:
        default_fhr_raw = [0]

    default_cycles = parse_cycle_list(default_cycle_raw) if default_cycle_raw else []
    default_fhr_cfg = parse_fhr_config(default_fhr_raw)

    raw_graphics = (
        recipe.get("graphics list")
        or recipe.get("graphics_list")
        or recipe.get("graphics name template")
        or recipe.get("graphics_name_template")
        or []
    )
    if isinstance(raw_graphics, str):
        raw_graphics = [raw_graphics]
    if not raw_graphics:
        raise ValueError("No 'graphics list' specified in recipe.")

    compiled: List[Tuple[str, List[str], Dict[str, Any], List[str], List[str]]] = []
    for item in raw_graphics:
        item_template = None
        item_cycles = default_cycles
        item_fhr_cfg = default_fhr_cfg
        item_wgf_list = default_wgf_list
        item_tile_list = default_tile_list

        if isinstance(item, str):
            item_template = item.strip()
        elif isinstance(item, dict):
            for k, v in item.items():
                item_template = str(k).strip()
                if isinstance(v, dict):
                    if "cycle" in v:
                        item_cycles = parse_cycle_list(v["cycle"])
                    if "fhr" in v:
                        item_fhr_cfg = parse_fhr_config(v["fhr"])
                    if "WGF" in v or "wgf" in v:
                        w_val = v.get("WGF") or v.get("wgf")
                        w_list = w_val if isinstance(w_val, list) else [w_val]
                        item_wgf_list = [str(x).strip() for x in w_list if str(x).strip()]
                    if "tile" in v:
                        t_val = v["tile"]
                        t_list = t_val if isinstance(t_val, list) else [t_val]
                        item_tile_list = [str(x).strip() for x in t_list if str(x).strip()]
                break

        if item_template:
            t_norm = re.sub(r"\.pg$", ".png", item_template, flags=re.IGNORECASE)
            compiled.append((t_norm, item_cycles, item_fhr_cfg, item_wgf_list, item_tile_list))

    return compiled


def build_download_targets(
    recipe: Dict[str, Any],
    output_base: Path,
    hpc_users: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """
    Builds per-COMROOT download target plans from recipe configuration.
    Supports `COMROOT` as a single string or a list of strings (potentially across
    multiple clusters and prefixes).
    """
    comroots = normalize_comroots(recipe.get("COMROOT"))
    if not comroots:
        raise ValueError("Missing 'COMROOT' in recipe.")

    resolved_users = load_hpc_users() if hpc_users is None else hpc_users
    compiled_specs = _compile_template_specs(recipe)
    raw_prefix = recipe.get("prefix")
    targets: List[Dict[str, Any]] = []

    for comroot_raw in comroots:
        if ":" in comroot_raw:
            hpc_cluster, remote_comroot = comroot_raw.split(":", 1)
            hpc_cluster = hpc_cluster.strip().lower()
            remote_comroot = remote_comroot.strip()
        else:
            hpc_cluster = "localhost"
            remote_comroot = comroot_raw

        username = resolved_users.get(hpc_cluster, "") if hpc_cluster != "localhost" else ""
        expname = extract_expname(remote_comroot)
        if raw_prefix:
            prefix = str(raw_prefix).strip()
        else:
            prefix = detect_prefix(remote_comroot, username, cluster=hpc_cluster)

        tasks: List[Tuple[str, Path]] = []
        clean_comroot = remote_comroot.rstrip("/")
        for t_norm, item_cycles, item_fhr_cfg, item_wgf_list, item_tile_list in compiled_specs:
            for cyc_str in item_cycles:
                pdy = cyc_str[:8]
                cyc = cyc_str[8:10]
                fhrs = resolve_fhrs_for_cycle(item_fhr_cfg, cyc_str)
                for wgf in item_wgf_list:
                    for tile in item_tile_list:
                        for fhr in fhrs:
                            valid_time = f"{pdy}{cyc}{fhr:02d}"
                            fhr_3d = f"{fhr:03d}"
                            fname = (
                                t_norm.replace("{tile}", tile)
                                .replace("{TILE}", tile)
                                .replace("{fhr}", fhr_3d)
                                .replace("{FHR}", fhr_3d)
                            )
                            rem_path = (
                                f"{clean_comroot}/{prefix}.{pdy}/{cyc}/graphics/{wgf}/{tile}/{valid_time}/{fname}"
                            )
                            local_path = output_base / expname / wgf / tile / cyc_str / fname
                            tasks.append((rem_path, local_path))

        targets.append({
            "comroot": comroot_raw,
            "cluster": hpc_cluster,
            "remote_comroot": remote_comroot,
            "username": username,
            "expname": expname,
            "prefix": prefix,
            "tasks": tasks,
        })

    return targets


def build_download_list(
    recipe: Dict[str, Any],
    output_base: Path,
    hpc_users: Optional[Dict[str, str]] = None,
) -> Tuple[str, str, str, str, List[Tuple[str, Path]]]:
    """
    Builds the combined list of (remote_path, local_path) tuples from recipe configuration.
    Supports `COMROOT` as a single string or a list of strings, top-level 'cycle'/'fhr'
    defaults, and per-graphic Style B overrides.
    Returns (hpc_cluster, username, expname, prefix, tasks).
    """
    targets = build_download_targets(recipe, output_base, hpc_users=hpc_users)
    all_tasks: List[Tuple[str, Path]] = []
    clusters: List[str] = []
    users: List[str] = []
    expnames: List[str] = []
    prefixes: List[str] = []

    for tgt in targets:
        all_tasks.extend(tgt["tasks"])
        if tgt["cluster"] not in clusters:
            clusters.append(tgt["cluster"])
        if tgt["username"] and tgt["username"] not in users:
            users.append(tgt["username"])
        if tgt["expname"] not in expnames:
            expnames.append(tgt["expname"])
        if tgt["prefix"] not in prefixes:
            prefixes.append(tgt["prefix"])

    return (
        clusters[0] if len(clusters) == 1 else ", ".join(clusters),
        users[0] if len(users) == 1 else (", ".join(users) if users else ""),
        expnames[0] if len(expnames) == 1 else ", ".join(expnames),
        prefixes[0] if len(prefixes) == 1 else ", ".join(prefixes),
        all_tasks,
    )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download selected pygraf graphics from NOAA RDHPCS over SSH tunnel.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "recipe",
        type=Path,
        help="Path to graphics download recipe YAML (e.g. recipes/download_pygraf_graphics.yaml)",
    )
    parser.add_argument(
        "-o", "--output-dir",
        type=Path,
        default=Path("graphics"),
        help="Base output directory (default: ./graphics)",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Re-download files even if already present locally",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List all planned downloads without transferring any files",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print verbose transfer details, including missing or uncopied files",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    recipe_path = args.recipe
    if not recipe_path.is_file():
        # Check inside recipes/
        cand = ROOT / "recipes" / recipe_path.name
        if cand.is_file():
            recipe_path = cand
        else:
            print(f"Error: Recipe file not found: {args.recipe}", file=sys.stderr)
            return 1

    recipe = load_recipe_yaml(recipe_path)
    if not recipe:
        print(f"Error: Empty or invalid recipe in {recipe_path}", file=sys.stderr)
        return 1

    try:
        targets = build_download_targets(recipe, args.output_dir)
    except Exception as e:
        print(f"Error parsing recipe: {e}", file=sys.stderr)
        return 1

    total_tasks = sum(len(t["tasks"]) for t in targets)
    unique_clusters = list(dict.fromkeys(t["cluster"] for t in targets))

    print("=" * 70)
    if len(targets) == 1:
        t0 = targets[0]
        print(f"[*] PYGRAF GRAPHICS DOWNLOAD: {t0['expname']}")
        print(f"[*] Cluster:       {t0['cluster']}")
        print(f"[*] Prefix:        {t0['prefix']}")
        print(f"[*] Local Target:  {args.output_dir / t0['expname']}")
    else:
        print(f"[*] PYGRAF GRAPHICS DOWNLOAD ({len(targets)} experiments)")
        print(f"[*] Clusters:      {', '.join(unique_clusters)}")
        for idx_t, t in enumerate(targets, 1):
            print(f"    [{idx_t}] {t['expname']} ({t['cluster']}, prefix={t['prefix']}, {len(t['tasks'])} files)")
    print(f"[*] Recipe:        {recipe_path.name}")
    print(f"[*] Total Targets: {total_tasks} files")
    if args.debug:
        print(f"[*] Debug Mode:    ENABLED")
    print("=" * 70)

    if args.debug:
        raw_fhr = recipe.get("fhr")
        if raw_fhr is None:
            def_cf = recipe.get("cycle and fhr") or {}
            raw_fhr = def_cf.get("fhr") if isinstance(def_cf, dict) else [0]
        fhr_cfg = parse_fhr_config(raw_fhr)
        print("\n[DEBUG] Default forecast lead hours (fhr) breakdown:")
        if fhr_cfg.get("legacy") is not None:
            print(f"  - All cycles: {len(fhr_cfg['legacy'])} leads: {fhr_cfg['legacy']}")
        else:
            for h_set, f_list in fhr_cfg.get("specific", []):
                h_str = ",".join(f"{h:02d}" for h in sorted(h_set))
                print(f"  - Cycles [{h_str}]: {len(f_list)} lead(s) (F{f_list[0]:03d}-F{f_list[-1]:03d})")
            if fhr_cfg.get("default") is not None:
                def_list = fhr_cfg["default"]
                print(f"  - Default cycles: {len(def_list)} lead(s) (F{def_list[0]:03d}-F{def_list[-1]:03d})")

    all_tasks = [item for t in targets for item in t["tasks"]]

    if args.dry_run:
        limit = len(all_tasks) if args.debug else 20
        print("\n[DRY RUN] Planned remote -> local file targets:")
        for r_path, l_path in all_tasks[:limit]:
            print(f"  {r_path}\n    -> {l_path}")
        if len(all_tasks) > limit:
            print(f"  ... and {len(all_tasks) - limit} more files (use --debug to display all).")
        print(f"\n[✓] Dry run completed for {len(all_tasks)} files.")
        return 0

    # Verify background HPC terminal sessions for all required clusters
    cluster_users: Dict[str, str] = {}
    for t in targets:
        if t["cluster"] not in cluster_users:
            cluster_users[t["cluster"]] = t["username"]
    unready_clusters: List[str] = []
    for cluster in unique_clusters:
        if args.debug:
            print(f"\n[*] [DEBUG] Verifying background HPC terminal session: {cluster}...", flush=True)
        user = cluster_users.get(cluster, "")
        with _SshCheckStatusBar(f"Verifying '{cluster}' HPC terminal session..."):
            session_ready = test_hpc_session_ready(cluster, user)
        if not session_ready:
            print(
                f"[*] HPC terminal session for '{cluster}' is not active; checking SSH connection...",
                flush=True,
            )
            if not _ensure_hpc_worker_if_master_alive(cluster, user, check_session_first=False):
                unready_clusters.append(cluster)

    if unready_clusters:
        print("\n" + "=" * 70, file=sys.stderr)
        for cluster in unready_clusters:
            print(f"[!] Background HPC terminal session for '{cluster}' is not ready.", file=sys.stderr)
        print("[!] Please establish the HPC session first in your terminal:", file=sys.stderr)
        for cluster in unready_clusters:
            print(f"        ush/ssh_helper.py {cluster}", file=sys.stderr)
        print("    and then rerun.", file=sys.stderr)
        print("=" * 70 + "\n", file=sys.stderr)
        return 1

    total_completed = 0
    total_already_cached = 0
    total_missing = 0
    total_failed = 0
    t_start = time.time()

    try:
        for idx_t, tgt in enumerate(targets, 1):
            expname = tgt["expname"]
            cluster = tgt["cluster"]
            username = tgt["username"]
            tasks = tgt["tasks"]

            if len(targets) > 1:
                print(f"\n[{idx_t}/{len(targets)}] Experiment: {expname} ({cluster})")

            pending_tasks = []
            already_cached = 0
            for r_path, l_path in tasks:
                if not args.update and l_path.exists() and l_path.stat().st_size > 0:
                    already_cached += 1
                else:
                    pending_tasks.append((r_path, l_path))

            total_already_cached += already_cached
            if already_cached > 0:
                print(f"[*] Local cache ({expname}): {already_cached} file(s) already exist (skipped).")

            if not pending_tasks:
                print(f"[✓] All {len(tasks)} files for '{expname}' are already downloaded and cached locally!")
                continue

            print(f"[*] Starting download of {len(pending_tasks)} file(s) for '{expname}' via streaming tar over SSH...\n")

            try:
                completed, missing, failed = stream_tar_pull(
                    username=username,
                    tasks=pending_tasks,
                    cluster=cluster,
                    debug=args.debug,
                )
                total_completed += completed
                total_missing += missing
                total_failed += failed
            finally:
                removed_count = cleanup_empty_directories(args.output_dir / expname)
                if args.debug and removed_count > 0:
                    print(f"\n[*] [DEBUG] Cleaned up {removed_count} empty directory(ies) under {expname}.")
    finally:
        try:
            if args.output_dir.exists() and not any(args.output_dir.iterdir()):
                args.output_dir.rmdir()
                if args.debug:
                    print(f"[*] [DEBUG] Removed base directory {args.output_dir} as it is empty.")
        except OSError:
            pass

    elapsed = time.time() - t_start
    print("\n" + "=" * 70)
    print(f"[✓] Completed in {elapsed:.1f}s")
    print(f"    - Downloaded:  {total_completed}")
    print(f"    - Already had: {total_already_cached}")
    if args.debug and total_missing > 0:
        print(f"    - Missing:     {total_missing}")
    if total_failed > 0:
        print(f"    - Errors:      {total_failed}")
    for tgt in targets:
        target_exp_dir = args.output_dir / tgt["expname"]
        if target_exp_dir.exists():
            print(f"    - Directory:   {target_exp_dir}")
        else:
            print(f"    - Directory:   (no files downloaded for {tgt['expname']}; empty directory removed)")
    print("=" * 70)

    return 0 if total_failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
