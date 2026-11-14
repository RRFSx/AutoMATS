"""
pygraf slides module for AutoMATS.

Handles:
  1. Parsing pygraf layout recipes (same cycle same fhr, same cycle different fhr, etc.).
  2. Resolving experiments and COMROOT locations on HPC systems (e.g. gaeac6).
  3. Verifying background HPC terminal session readiness; skipping and prompting users if offline.
  4. Pulling remote graphic files via streaming tar over the background HPC terminal session.
  5. Generating OBS and missing-image placeholders.
  6. Rendering pygraf slides into the presentation deck.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from io import BytesIO
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

from automats.mrms import format_mrms_filename, is_valid_mrms_file

try:
    import yaml
except ImportError:
    yaml = None

from automats.config import Experiment, Plan, title_contains_variable, split_title_variable_spans

ORDINALS = {
    "first": 0, "1st": 0, "1": 0,
    "second": 1, "2nd": 1, "2": 1,
    "third": 2, "3rd": 2, "3": 2,
    "fourth": 3, "4th": 3, "4": 3,
    "fifth": 4, "5th": 4, "5": 4,
    "sixth": 5, "6th": 5, "6": 5,
    "seventh": 6, "7th": 6, "7": 6,
    "eighth": 7, "8th": 7, "8": 7,
    "ninth": 8, "9th": 8, "9": 8,
    "tenth": 9, "10th": 9, "10": 9,
}


def _resolve_ssh_helper_path() -> Path:
    """Returns path to ush/ssh_helper.py relative to AutoMATS root."""
    script_dir = Path(__file__).resolve().parent
    automats_root = script_dir.parent.parent
    return automats_root / "ush" / "ssh_helper.py"


def find_hpc_config_file() -> Optional[Path]:
    """Finds .hpc_config path in CWD or AutoMATS repository root."""
    cwd_dot = Path.cwd() / ".hpc_config"
    if cwd_dot.is_file():
        return cwd_dot
    script_dir = Path(__file__).resolve().parent
    automats_root = script_dir.parent.parent
    root_dot = automats_root / ".hpc_config"
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
    """Tests whether the persistent background HPC terminal session is active for `cluster`."""
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


def resolve_experiment(ref: str, experiments: tuple[Experiment, ...]) -> Optional[Experiment]:
    """Resolves an experiment reference ('first', 'second', experiment name/short name/label)."""
    norm = ref.strip().lower()
    if norm in ORDINALS:
        idx = ORDINALS[norm]
        if idx < len(experiments):
            return experiments[idx]
        return None
    for exp in experiments:
        if (
            exp.name.lower() == norm
            or exp.label.lower() == norm
            or (exp.short_name and exp.short_name.lower() == norm)
        ):
            return exp
    return None


def parse_graphics_name(filename: str) -> Tuple[str, str, int]:
    """
    Given 'cref_full_sfc_f006.png' (or '.pg'):
    Normalizes extension to .png, extracts tile (default 'full'),
    and extracts lead hour integer (e.g. 6).
    Returns (normalized_name, tile, fhr).
    """
    clean_name = re.sub(r"\.pg$", ".png", filename.strip(), flags=re.IGNORECASE)
    if not clean_name.lower().endswith(".png"):
        clean_name += ".png"

    # Strip any leading 10-digit cycle prefix (e.g. 2024051009_cref_full_sfc_f002.png -> cref_full_sfc_f002.png)
    clean_name = re.sub(r"^\d{10}_", "", clean_name)

    m_fhr = re.search(r"_f(\d+)\.png$", clean_name, re.IGNORECASE)
    fhr = int(m_fhr.group(1)) if m_fhr else 0

    parts = clean_name.split("_")
    tile = parts[1] if len(parts) >= 3 else "full"
    return clean_name, tile, fhr


def compute_valid_time(cycle: str | int, fhr: int) -> Tuple[str, str, str]:
    """
    Given cycle (e.g. 2024050612) and forecast hour (e.g. 2):
    Returns (pdy, cyc, valid_time_dir).
    valid_time_dir is <PDY><cyc><fhr:02d> (e.g. 202405061202).
    """
    c_str = str(cycle).strip()
    if len(c_str) < 10:
        raise ValueError(f"Cycle must be at least 10 characters (YYYYMMDDHH), got: {c_str}")
    pdy = c_str[:8]
    cyc = c_str[8:10]
    valid_time_dir = f"{pdy}{cyc}{fhr:02d}"
    return pdy, cyc, valid_time_dir


def resolve_obs_cell_time(
    spec: Dict[str, Any],
    row_items: List[Any],
    item_str: str,
) -> Tuple[str, datetime]:
    """
    Resolves observation variable (e.g. 'cref') and valid datetime for an 'obs' cell.
    - 'same valid time': takes fhr and variable from 'graphics name' (e.g. cref_full_sfc_f006.png).
    - 'same cycle': extracts companion forecast fhr and variable from the same row.
    Supports explicit item specifiers like 'obs_f06', 'obs_cref_f06', or 'obs_202405070600'.
    """
    cycle_val = spec.get("cycle")
    c_str = str(cycle_val).strip() if cycle_val else ""
    try:
        cycle_dt = datetime.strptime(c_str[:10], "%Y%m%d%H") if len(c_str) >= 10 else datetime.now()
    except ValueError:
        cycle_dt = datetime.now()

    var = "cref"
    slide_type = str(spec.get("type", "same valid time")).strip().lower().replace("_", " ")

    # Check for explicit timestamp: obs_202405070600 or obs_cref_202405070600
    m_ts = re.search(r"obs(?:_([A-Za-z0-9_-]+))?[_-](\d{10}|\d{12})$", item_str, re.IGNORECASE)
    if m_ts:
        if m_ts.group(1):
            var = m_ts.group(1).lower()
        ts = m_ts.group(2)
        fmt = "%Y%m%d%H%M" if len(ts) == 12 else "%Y%m%d%H"
        try:
            return var, datetime.strptime(ts, fmt)
        except ValueError:
            pass

    is_compare_exps = (
        ("graphics name" in spec and "experiment" not in spec)
        or "same fhr" in slide_type
        or slide_type == "same valid time"
    )
    if is_compare_exps:
        g_name = spec.get("graphics name", "cref_full_sfc_f006.png")
        clean_name, tile, fhr = parse_graphics_name(g_name)
        if "_" in clean_name:
            var = clean_name.split("_")[0].lower()
        return var, cycle_dt + timedelta(hours=fhr)
    else:
        # 'same cycle': find companion forecast item in row_items
        fhr = 0
        for other in row_items:
            other_str = str(other).strip()
            if other_str.lower().startswith("obs"):
                continue
            if re.search(r"_f\d+", other_str, re.IGNORECASE) or other_str.lower().endswith((".png", ".pg")):
                m_cyc = re.match(r"^(\d{10})_", other_str)
                if m_cyc:
                    try:
                        cycle_dt = datetime.strptime(m_cyc.group(1), "%Y%m%d%H")
                    except ValueError:
                        pass
                clean_name, tile, item_fhr = parse_graphics_name(other_str)
                fhr = item_fhr
                if "_" in clean_name:
                    var = clean_name.split("_")[0].lower()
                break
        return var, cycle_dt + timedelta(hours=fhr)


def resolve_obs_tile(
    spec: Dict[str, Any],
    row_items: List[Any],
    item_str: str,
) -> str:
    """
    Resolves the geographic tile ('full', 'NC', 'EastCO', etc.) for an obs item.
    Precedence:
    1. Explicit obs item suffix: 'obs:EastCO', 'obs_EastCO', 'obs(EastCO)', 'obs[EastCO]'.
    2. Slide spec 'tile' override.
    3. Companion graphics in the same row (e.g. 'cref_EastCO_sfc_f006.png').
    4. Slide spec 'graphics name' (e.g. 'cref_NC_sfc_f006.png').
    Defaults to 'full'.
    """
    s = str(item_str).strip()
    # Check if obs string embeds a tile: obs:EastCO, obs_EastCO, obs/EastCO, obs[EastCO], obs(EastCO)
    # Ignore pure timestamps like obs_202405070600 or forecast leads like obs_f06
    m_explicit = re.match(r"^obs[:_/\(\[]([A-Za-z0-9_-]+)[\)\]]?$", s, re.IGNORECASE)
    if m_explicit:
        cand = m_explicit.group(1).strip()
        if not re.match(r"^(?:f\d+|\d{8,12}|cref|ref)$", cand, re.IGNORECASE):
            return cand

    # Slide spec 'tile' override
    if spec.get("tile"):
        return str(spec["tile"]).strip()

    # Companion graphic in row_items
    for companion in row_items:
        c_str = str(companion).strip()
        if not c_str.lower().startswith("obs") and ("." in c_str or "_f" in c_str):
            _, tile_extracted, _ = parse_graphics_name(c_str)
            if tile_extracted:
                return tile_extracted

    # Slide spec 'graphics name'
    if spec.get("graphics name"):
        _, tile_extracted, _ = parse_graphics_name(str(spec["graphics name"]))
        if tile_extracted:
            return tile_extracted

    return "full"


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


def build_remote_path(
    comroot: str,
    prefix: str,
    cycle: str | int,
    wgf: str,
    tile: str,
    fhr: int,
    graphics_name: str,
) -> str:
    """
    Constructs:
    {COMROOT}/{prefix}.{PDY}/{CYC}/graphics/{WGF}/{tile}/{valid_time}/{graphics_name}
    """
    pdy, cyc, valid_time_dir = compute_valid_time(cycle, fhr)
    clean_comroot = comroot.rstrip("/")
    if not prefix:
        prefix = detect_prefix_from_path(clean_comroot) or "rrfs"
    return f"{clean_comroot}/{prefix}.{pdy}/{cyc}/graphics/{wgf}/{tile}/{valid_time_dir}/{graphics_name}"


def _create_placeholder_png(dest_path: Path, title: str, subtitle: str) -> None:
    """Creates a clean placeholder PNG image using PIL."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1200, 900), color=(242, 245, 249))
    draw = ImageDraw.Draw(img)

    # Rounded rectangle border
    border_color = (180, 195, 210)
    draw.rectangle([(15, 15), (1185, 885)], outline=border_color, width=3)

    # Text rendering
    try:
        font_large = ImageFont.load_default(size=40)
        font_small = ImageFont.load_default(size=24)
    except TypeError:
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()

    text_color = (60, 75, 95)
    sub_color = (110, 125, 145)

    bbox1 = draw.textbbox((0, 0), title, font=font_large)
    w1, h1 = bbox1[2] - bbox1[0], bbox1[3] - bbox1[1]

    bbox2 = draw.textbbox((0, 0), subtitle, font=font_small)
    w2, h2 = bbox2[2] - bbox2[0], bbox2[3] - bbox2[1]

    total_h = h1 + 25 + h2
    y1 = (900 - total_h) / 2
    x1 = (1200 - w1) / 2

    y2 = y1 + h1 + 25
    x2 = (1200 - w2) / 2

    draw.text((x1, y1), title, fill=text_color, font=font_large)
    draw.text((x2, y2), subtitle, fill=sub_color, font=font_small)

    img.save(dest_path, "PNG")


def get_obs_placeholder(output_dir: Path) -> Path:
    """Returns path to the generated MRMS OBS placeholder image."""
    path = output_dir / "graphics" / "mrms_obs_placeholder.png"
    if not path.exists():
        _create_placeholder_png(path, "MRMS OBS", "Radar Reflectivity Observation (Pending)")
    return path


def get_missing_placeholder(output_dir: Path, name: str) -> Path:
    """Returns path to a placeholder image when a graphic file is not found."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", name)
    path = output_dir / "graphics" / "placeholders" / f"missing_{slug}.png"
    if not path.exists():
        _create_placeholder_png(path, "Figure Not Found", name)
    return path


def cleanup_empty_directories(root_dir: Path) -> int:
    """Recursively removes empty directories under root_dir bottom-up."""
    removed_count = 0
    if not root_dir.exists() or not root_dir.is_dir():
        return 0

    for dirpath, dirnames, filenames in os.walk(str(root_dir), topdown=False):
        d = Path(dirpath)
        try:
            items = [item for item in d.iterdir() if item.name != ".DS_Store"]
            if not items:
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
                        stream_failed = False

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
                                stream_failed = True
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

                        if stream_failed:
                            for dest in dests[primary_idx + 1:]:
                                d_key = (norm, os.path.abspath(str(dest)))
                                if d_key not in error_keys and d_key not in received_keys:
                                    error_keys.add(d_key)
                                    errors += task_counts.get(d_key, 1)
                                if debug:
                                    print(f"  [DEBUG] Error writing {dest}: aborted after stream failure", file=sys.stderr)
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

    if total_tasks > 0 and (completed + errors < total_tasks or (completed + errors) % 10 != 0):
        pct = int(100 * (completed + errors) / total_tasks)
        if missing > 0:
            print(f"  Progress: {total_tasks}/{total_tasks} ({pct}%) [{completed} ok, {missing} missing]", flush=True)
        else:
            print(f"  Progress: {total_tasks}/{total_tasks} ({pct}%) [{completed} ok]", flush=True)

    # In debug mode, list missing files
    if debug and missing > 0:
        for r_path, _ in missing_tasks:
            print(f"  [DEBUG] Missing on {cluster}: {Path(r_path).name}\n          Remote path: {r_path}", file=sys.stderr)

    return completed, missing, errors


def batch_pull_graphics_files(
    tasks: List[Dict[str, Any]],
    max_workers: int = 1,
    debug: bool = False,
) -> Dict[str, bool]:
    """
    Downloads uncached graphic files using single-session streaming tar over SSH per HPC endpoint.
    Eliminates SSH multiplexer connection exhaustion and OpenSSH session rate limits.
    """
    results: Dict[str, bool] = {}
    pending: List[Dict[str, Any]] = []

    for t in tasks:
        loc = Path(t["local_path"])
        if loc.exists() and loc.stat().st_size > 0:
            results[str(loc)] = True
        else:
            pending.append(t)

    if not pending:
        return results

    # Deduplicate pending tasks by (cluster, remote_path, str(local_path))
    unique_pending: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for t in pending:
        key = (t.get("cluster", "HPC"), t["remote_path"], str(t["local_path"]))
        unique_pending[key] = t

    # Group tasks by remote endpoint: (username, cluster)
    endpoints: Dict[Tuple[str, str], List[Tuple[str, Path]]] = {}
    for t in unique_pending.values():
        ep_key = (t["username"], t.get("cluster", "HPC"))
        if ep_key not in endpoints:
            endpoints[ep_key] = []
        endpoints[ep_key].append((t["remote_path"], Path(t["local_path"])))

    try:
        for (user, cluster), cluster_tasks in endpoints.items():
            print(
                f"[*] Downloading {len(cluster_tasks)} graphics figure(s) from {cluster} via streaming tar...",
                flush=True,
            )
            stream_tar_pull(
                username=user,
                tasks=cluster_tasks,
                cluster=cluster,
                debug=debug,
            )
            for _, l_path in cluster_tasks:
                results[str(l_path)] = l_path.exists() and l_path.stat().st_size > 0
    finally:
        # Clean up empty experiment directories if no files were downloaded
        for t in unique_pending.values():
            loc = Path(t["local_path"])
            cleanup_empty_directories(loc.parent)

    return results


def resolve_graphics_recipe_path(recipe_name: str, base_recipe_path: Optional[Path]) -> Optional[Path]:
    """Finds the graphics recipe YAML file."""
    candidates = []
    direct = Path(recipe_name)
    if direct.is_absolute() and direct.exists():
        return direct
    candidates.append(direct)

    if base_recipe_path is not None:
        candidates.append(base_recipe_path.parent / recipe_name)
        candidates.append(base_recipe_path.parent / "recipes" / recipe_name)

    cwd = Path.cwd()
    candidates.append(cwd / recipe_name)
    candidates.append(cwd / "recipes" / recipe_name)

    script_dir = Path(__file__).resolve().parent
    automats_root = script_dir.parent.parent
    candidates.append(automats_root / "recipes" / recipe_name)

    for cand in candidates:
        if cand.exists():
            return cand
    return None


def crop_pygraf_header(im: Any) -> Any:
    """Crops the redundant top title/caption block from a pygraf figure.

    In pygraf figures, the top caption (variable, cycle, forecast hour) sits
    above the black map bounding box, which begins around y=67 (or 10-18% of height).
    """
    try:
        rgb = im.convert("RGB")
        w, h = im.size
        min_y = int(h * 0.08)
        max_y = int(h * 0.18)
        best_y = None
        max_dark = 0
        for y in range(min_y, max_y):
            dark = sum(
                1 for x in range(10, w - 10)
                if rgb.getpixel((x, y))[0] < 50 and rgb.getpixel((x, y))[1] < 50 and rgb.getpixel((x, y))[2] < 50
            )
            if dark > max_dark and dark > w * 0.15:
                max_dark = dark
                best_y = y
        if best_y is not None:
            return im.crop((0, best_y, w, h))
    except Exception:
        pass
    return im


def process_graphics_slides(
    plan: Plan,
    output_dir: Path,
    deck: Any,
    stats: Optional[Dict[str, int]] = None,
    log: Optional[Any] = None,
) -> int:
    """
    Checks HPC readiness, downloads necessary graphics files from HPC,
    and appends pygraf slides to the PowerPoint presentation deck.
    Returns the number of pygraf slides successfully added.
    """
    if stats is not None:
        stats["saved"] = 0

    def _emit_warning(msg: str) -> None:
        if callable(log):
            log(msg)
        else:
            print(msg, file=sys.stderr)

    if not plan.pygraf_graphics or not plan.pygraf_graphics.enable:
        return 0

    recipe_name = plan.pygraf_graphics.recipe
    if not recipe_name:
        _emit_warning("[!] Warning: 'pygraf slides' enabled but no 'recipe' file specified.")
        return 0

    recipe_file = resolve_graphics_recipe_path(recipe_name, plan.recipe_path)
    if not recipe_file or not recipe_file.exists():
        _emit_warning(f"[!] Warning: pygraf recipe file '{recipe_name}' not found.")
        return 0

    if yaml is None:
        _emit_warning("[!] Warning: PyYAML is required to read pygraf recipe; activate the virtual environment.")
        return 0

    try:
        with recipe_file.open(encoding="utf-8") as f:
            raw_data = yaml.safe_load(f)
    except Exception as e:
        _emit_warning(f"[!] Warning: Failed to parse pygraf recipe '{recipe_file}': {e}")
        return 0

    slides_config = raw_data.get("pygraf slides")
    if not slides_config or not isinstance(slides_config, list):
        _emit_warning(f"[!] Warning: No 'pygraf slides' list found in '{recipe_file}'.")
        return 0

    # 1. Identify all required HPC clusters from experiments
    hpc_users = load_hpc_users()
    needed_clusters: Dict[str, str] = {}
    for exp in plan.experiments:
        cluster = exp.hpc_cluster
        if cluster and cluster not in needed_clusters:
            needed_clusters[cluster] = hpc_users.get(cluster.lower(), "")

    # 2. Check if background terminal sessions to needed HPC clusters are active
    bound_progress = getattr(log, "__self__", None)
    has_outer_progress = (
        bound_progress is not None
        and hasattr(bound_progress, "update")
        and hasattr(bound_progress, "_draw")
    )
    unready_clusters: List[str] = []
    for cluster, user in needed_clusters.items():
        if has_outer_progress:
            bound_progress.update(f"Verifying '{cluster}' HPC terminal session")
        with _SshCheckStatusBar(
            f"Verifying '{cluster}' HPC terminal session...",
            enabled=not has_outer_progress,
        ):
            session_ready = test_hpc_session_ready(cluster, user)
        if not session_ready:
            msg = f"[*] HPC terminal session for '{cluster}' is not active; checking SSH connection..."
            if callable(log):
                log(msg)
            else:
                print(msg, flush=True)
            if has_outer_progress:
                bound_progress.update(f"Checking '{cluster}' SSH connection")
            if not _ensure_hpc_worker_if_master_alive(
                cluster,
                user,
                check_session_first=False,
                show_status=not has_outer_progress,
            ):
                unready_clusters.append(cluster)

    if unready_clusters:
        if callable(log):
            _emit_warning("=" * 70)
        else:
            print("\n" + "=" * 70, file=sys.stderr)
        for cluster in unready_clusters:
            _emit_warning(f"[!] Background HPC terminal session for '{cluster}' is not ready.")
        _emit_warning("[!] Skipping pygraf slides.")
        _emit_warning("[!] To enable pygraf slides, please establish the HPC session first:")
        for cluster in unready_clusters:
            _emit_warning(f"        ush/ssh_helper.py {cluster}")
        _emit_warning("    and then rerun.")
        if callable(log):
            _emit_warning("=" * 70)
        else:
            print("=" * 70 + "\n", file=sys.stderr)
        return 0

    # 3. Phase 1: Collect and batch-pull all needed graphic figures in parallel
    transfer_tasks: List[Dict[str, Any]] = []
    mrms_tasks: List[Dict[str, Any]] = []
    for spec in slides_config:
        slide_type = str(spec.get("type", "same valid time")).strip().lower().replace("_", " ")
        prefix = spec.get("prefix")
        wgf = spec.get("WGF", "det")
        tile_override = spec.get("tile")
        cycle = spec.get("cycle")
        num_rows = spec.get("number of rows", 2)

        for r_num in range(1, num_rows + 1):
            row_items = spec.get(f"row{r_num}", [])
            if not isinstance(row_items, list):
                continue
            for item in row_items:
                item_str = str(item).strip()
                if item_str.lower().startswith("obs"):
                    var, valid_dt = resolve_obs_cell_time(spec, row_items, item_str)
                    tile = resolve_obs_tile(spec, row_items, item_str)
                    dest_obs = output_dir / "graphics" / "obs" / format_mrms_filename(var, valid_dt, tile=tile)
                    mrms_tasks.append({
                        "var": var,
                        "valid_dt": valid_dt,
                        "tile": tile,
                        "dest_path": dest_obs,
                    })
                    continue
                is_compare_exps = (
                    ("graphics name" in spec and "experiment" not in spec)
                    or "same fhr" in slide_type
                    or slide_type == "same valid time"
                )
                if is_compare_exps:
                    exp_obj = resolve_experiment(item_str, plan.experiments)
                    g_name = spec.get("graphics name", "cref_full_sfc_f006.png")
                    clean_name, tile_extracted, fhr = parse_graphics_name(g_name)
                    tile = tile_override or tile_extracted
                    if exp_obj and exp_obj.hpc_cluster and exp_obj.remote_comroot:
                        cluster = exp_obj.hpc_cluster
                        user = needed_clusters.get(cluster, "")
                        rem_path = build_remote_path(
                            comroot=exp_obj.remote_comroot,
                            prefix=prefix,
                            cycle=cycle,
                            wgf=wgf,
                            tile=tile,
                            fhr=fhr,
                            graphics_name=clean_name,
                        )
                        local_img = output_dir / "graphics" / exp_obj.name / f"{cycle}_{fhr:03d}_{clean_name}"
                        transfer_tasks.append({
                            "username": user,
                            "remote_path": rem_path,
                            "local_path": local_img,
                            "cluster": cluster,
                            "clean_name": clean_name,
                        })
                else:
                    exp_ref = spec.get("experiment", "first")
                    exp_obj = resolve_experiment(exp_ref, plan.experiments)
                    m_item_cyc = re.match(r"^(\d{10})_", item_str)
                    row_cycle = m_item_cyc.group(1) if m_item_cyc else cycle
                    clean_name, tile_extracted, fhr = parse_graphics_name(item_str)
                    tile = tile_override or tile_extracted
                    if exp_obj and exp_obj.hpc_cluster and exp_obj.remote_comroot:
                        cluster = exp_obj.hpc_cluster
                        user = needed_clusters.get(cluster, "")
                        rem_path = build_remote_path(
                            comroot=exp_obj.remote_comroot,
                            prefix=prefix,
                            cycle=row_cycle,
                            wgf=wgf,
                            tile=tile,
                            fhr=fhr,
                            graphics_name=clean_name,
                        )
                        local_img = output_dir / "graphics" / exp_obj.name / f"{row_cycle}_{fhr:03d}_{clean_name}"
                        transfer_tasks.append({
                            "username": user,
                            "remote_path": rem_path,
                            "local_path": local_img,
                            "cluster": cluster,
                            "clean_name": clean_name,
                        })

    newly_saved_graphics = 0

    if transfer_tasks:
        uncached_remote_dests: List[Path] = []
        seen_dests: set[str] = set()
        for t in transfer_tasks:
            loc = Path(t["local_path"])
            key = str(loc)
            if key in seen_dests:
                continue
            seen_dests.add(key)
            if not (loc.exists() and loc.stat().st_size > 0):
                uncached_remote_dests.append(loc)

        batch_pull_graphics_files(transfer_tasks)
        newly_saved_graphics += sum(
            1 for loc in uncached_remote_dests if loc.exists() and loc.stat().st_size > 0
        )

    # Pull uncached MRMS observation figures
    if mrms_tasks:
        unique_mrms_map = {str(t["dest_path"]): t for t in mrms_tasks}
        unique_mrms = list(unique_mrms_map.values())
        uncached_mrms = [t for t in unique_mrms if not is_valid_mrms_file(t["dest_path"])]
        if uncached_mrms:
            try:
                from automats.mrms import fetch_mrms_images
                fetch_mrms_images(uncached_mrms, output_dir=output_dir)
                newly_saved_graphics += sum(
                    1 for t in uncached_mrms if is_valid_mrms_file(t["dest_path"])
                )
            except Exception as e:
                _emit_warning(f"[!] Warning: Could not fetch MRMS observation figures: {e}")

    if stats is not None:
        stats["saved"] = newly_saved_graphics

    # 4. Phase 2: Render each pygraf slide into PowerPoint deck
    if has_outer_progress:
        bound_progress.update("Building pygraf slides")

    from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
    from pptx.util import Inches, Pt
    from PIL import Image

    margin = Inches(0.4)
    content_top = Inches(0.82)
    content_bottom = Inches(7.20)
    avail_height = content_bottom - content_top
    caption_height = Inches(0.3)

    added_count = 0
    warned_unresolved_exps: set[str] = set()
    warned_missing_comroot: set[str] = set()

    for slide_idx, spec in enumerate(slides_config, 1):
        slide_type = str(spec.get("type", "same valid time")).strip().lower().replace("_", " ")
        title = spec.get("title", "Composite Reflectivity")
        prefix = spec.get("prefix", "rrfs")
        wgf = spec.get("WGF", "det")
        tile_override = spec.get("tile")
        cycle = spec.get("cycle")
        num_rows = spec.get("number of rows", 2)

        # Collect rows
        rows_data = []
        for r_num in range(1, num_rows + 1):
            row_items = spec.get(f"row{r_num}", [])
            if isinstance(row_items, list):
                rows_data.append(row_items)

        if not rows_data:
            continue

        slide = deck.slides.add_slide(deck.slide_layouts[6])

        # Smartly wrap and structure title to avoid overlay with [ ⌂ Overview ] button
        is_same_valid = (
            ("graphics name" in spec and "experiment" not in spec)
            or "same fhr" in slide_type
            or slide_type == "same valid time"
        )
        if is_same_valid:
            g_name = spec.get("graphics name", "")
            clean_name, tile, fhr = parse_graphics_name(g_name)
            title_line1 = title
            title_line2 = f"Cycle: {cycle} F{fhr:03d}"
        else:
            exp_ref = spec.get("experiment", "first")
            exp_obj = resolve_experiment(exp_ref, plan.experiments)
            exp_name = exp_obj.display_name if exp_obj else exp_ref
            title_line1 = title
            title_line2 = f"{exp_name}   •   Cycle: {cycle}" if cycle else f"{exp_name}"

        # Centered heading with symmetric 1.6" margins to safely clear top-right [ ⌂ Overview ] button
        heading_w = deck.slide_width - Inches(3.2)
        heading_left = Inches(1.6)
        heading = slide.shapes.add_textbox(heading_left, Inches(0.10), heading_w, Inches(0.70))
        heading.text_frame.word_wrap = True
        heading.text_frame.margin_top = heading.text_frame.margin_bottom = 0
        heading.text_frame.margin_left = heading.text_frame.margin_right = 0

        p1 = heading.text_frame.paragraphs[0]
        p1.text = ""
        p1.alignment = PP_ALIGN.CENTER
        spans = split_title_variable_spans(title_line1)
        if spans:
            for span_text, is_var in spans:
                run = p1.add_run()
                run.text = span_text
                run.font.size = Pt(19)
                run.font.bold = True
                if is_var:
                    try:
                        from pptx.dml.color import RGBColor
                        run.font.color.rgb = RGBColor(0, 0, 255)
                    except Exception:
                        pass
        else:
            run = p1.add_run()
            run.text = title_line1
            run.font.size = Pt(19)
            run.font.bold = True

        p2 = heading.text_frame.add_paragraph()
        p2.alignment = PP_ALIGN.CENTER
        try:
            from pptx.dml.color import RGBColor
            slate_rgb = RGBColor(70, 85, 105)
            blue_rgb = RGBColor(0, 0, 255)
        except Exception:
            slate_rgb = None
            blue_rgb = None

        if is_same_valid:
            p2.text = title_line2
            p2.font.size = Pt(12.5)
            p2.font.bold = True
            if slate_rgb is not None:
                p2.font.color.rgb = slate_rgb
        else:
            r_exp = p2.add_run()
            r_exp.text = str(exp_name)
            r_exp.font.size = Pt(12.5)
            r_exp.font.bold = True
            if blue_rgb is not None:
                r_exp.font.color.rgb = blue_rgb

            if cycle:
                r_cycle = p2.add_run()
                r_cycle.text = f"   •   Cycle: {cycle}"
                r_cycle.font.size = Pt(12.5)
                r_cycle.font.bold = True
                if slate_rgb is not None:
                    r_cycle.font.color.rgb = slate_rgb

        # Determine whether to crop redundant pygraf caption/header block (variable, cycle, fhr)
        is_cref = "composite" in title.lower() and "refl" in title.lower()
        has_cycle_and_fhr = "cycle" in title_line2.lower() and bool(re.search(r"\bf\d+", title_line2, re.IGNORECASE))
        should_crop_header = spec.get("crop_header", (is_cref and has_cycle_and_fhr))

        # Determine whether to show per-plot captions
        spec_caption_opt = spec.get("captions") if "captions" in spec else spec.get("caption")
        show_captions = bool(spec_caption_opt) if spec_caption_opt is not None else True

        caption_height = Inches(0.24) if show_captions else Inches(0.0)
        row_gap = Inches(0.10)
        row_height = (avail_height - row_gap * (len(rows_data) - 1)) // len(rows_data)

        for row_idx, row_items in enumerate(rows_data):
            row_top = content_top + row_idx * (row_height + row_gap)
            col_gap = Inches(0.15)
            cell_width = (deck.slide_width - 2 * margin - col_gap * (len(row_items) - 1)) // len(row_items)
            img_avail_h = row_height - caption_height - Inches(0.02) if show_captions else row_height

            for col_idx, item in enumerate(row_items):
                item_str = str(item).strip()
                cell_left = margin + col_idx * (cell_width + col_gap)

                if item_str.lower().startswith("obs"):
                    tile = resolve_obs_tile(spec, row_items, item_str)
                    caption_text = f"MRMS OBS ({tile})" if tile.lower() not in ("full", "conus") else "MRMS OBS"
                    var, valid_dt = resolve_obs_cell_time(spec, row_items, item_str)
                    obs_target = output_dir / "graphics" / "obs" / format_mrms_filename(var, valid_dt, tile=tile)
                    if is_valid_mrms_file(obs_target):
                        local_img = obs_target
                    else:
                        local_img = get_obs_placeholder(output_dir)
                elif is_same_valid:
                    # item is experiment reference (e.g. 'first', 'second')
                    exp_obj = resolve_experiment(item_str, plan.experiments)
                    caption_text = exp_obj.display_name if exp_obj else item_str
                    g_name = spec.get("graphics name", "cref_full_sfc_f006.png")
                    clean_name, tile_extracted, fhr = parse_graphics_name(g_name)

                    if exp_obj and exp_obj.hpc_cluster and exp_obj.remote_comroot:
                        local_img = output_dir / "graphics" / exp_obj.name / f"{cycle}_{fhr:03d}_{clean_name}"
                        if not (local_img.exists() and local_img.stat().st_size > 0):
                            local_img = get_missing_placeholder(output_dir, clean_name)
                    else:
                        if not exp_obj:
                            if item_str not in warned_unresolved_exps:
                                warned_unresolved_exps.add(item_str)
                                _emit_warning(f"[!] Warning: Experiment reference '{item_str}' could not be resolved.")
                        elif not exp_obj.COMROOT:
                            if exp_obj.name not in warned_missing_comroot:
                                warned_missing_comroot.add(exp_obj.name)
                                _emit_warning(f"[!] Warning: Experiment '{exp_obj.name}' has no COMROOT configured.")
                        elif not exp_obj.hpc_cluster or not exp_obj.remote_comroot:
                            if exp_obj.name not in warned_missing_comroot:
                                warned_missing_comroot.add(exp_obj.name)
                                _emit_warning(
                                    f"[!] Warning: Experiment '{exp_obj.name}' COMROOT '{exp_obj.COMROOT}' "
                                    "cannot infer an HPC cluster; prefix with <cluster>: (e.g. ursa:/path)."
                                )
                        local_img = get_missing_placeholder(output_dir, clean_name)
                else:
                    # 'same cycle': item is graphics filename (e.g. 'cref_full_sfc_f001.png')
                    exp_ref = spec.get("experiment", "first")
                    exp_obj = resolve_experiment(exp_ref, plan.experiments)
                    m_item_cyc = re.match(r"^(\d{10})_", item_str)
                    row_cycle = m_item_cyc.group(1) if m_item_cyc else cycle
                    clean_name, tile_extracted, fhr = parse_graphics_name(item_str)
                    caption_text = f"Cycle {row_cycle} F{fhr:03d}" if m_item_cyc else f"F{fhr:03d}"

                    if exp_obj and exp_obj.hpc_cluster and exp_obj.remote_comroot:
                        local_img = output_dir / "graphics" / exp_obj.name / f"{row_cycle}_{fhr:03d}_{clean_name}"
                        if not (local_img.exists() and local_img.stat().st_size > 0):
                            local_img = get_missing_placeholder(output_dir, clean_name)
                    else:
                        if not exp_obj:
                            ref_key = str(exp_ref)
                            if ref_key not in warned_unresolved_exps:
                                warned_unresolved_exps.add(ref_key)
                                _emit_warning(f"[!] Warning: Experiment reference '{ref_key}' could not be resolved.")
                        elif not exp_obj.COMROOT:
                            if exp_obj.name not in warned_missing_comroot:
                                warned_missing_comroot.add(exp_obj.name)
                                _emit_warning(f"[!] Warning: Experiment '{exp_obj.name}' has no COMROOT configured.")
                        elif not exp_obj.hpc_cluster or not exp_obj.remote_comroot:
                            if exp_obj.name not in warned_missing_comroot:
                                warned_missing_comroot.add(exp_obj.name)
                                _emit_warning(
                                    f"[!] Warning: Experiment '{exp_obj.name}' COMROOT '{exp_obj.COMROOT}' "
                                    "cannot infer an HPC cluster; prefix with <cluster>: (e.g. ursa:/path)."
                                )
                        local_img = get_missing_placeholder(output_dir, clean_name)

                # Measure and position image
                try:
                    with Image.open(local_img) as im:
                        if should_crop_header and not item_str.lower().startswith("obs"):
                            cropped_im = crop_pygraf_header(im)
                        else:
                            cropped_im = im.copy()
                        pw, ph = cropped_im.size
                        buf = BytesIO()
                        cropped_im.save(buf, format="PNG")
                        img_bytes = buf.getvalue()
                except Exception:
                    pw, ph = 1200, 900
                    try:
                        img_bytes = local_img.read_bytes()
                    except Exception:
                        img_bytes = b""

                scale = min(cell_width / pw, img_avail_h / ph)
                w = max(1, int(pw * scale))
                h = max(1, int(ph * scale))
                img_left = cell_left + (cell_width - w) // 2
                if show_captions:
                    img_top = row_top + caption_height + (img_avail_h - h) // 2

                    # Add caption centered above the figure
                    cap_box = slide.shapes.add_textbox(cell_left, row_top, cell_width, caption_height)
                    cap_tf = cap_box.text_frame
                    cap_tf.word_wrap = True
                    cap_tf.margin_left = cap_tf.margin_right = cap_tf.margin_top = cap_tf.margin_bottom = 0
                    cap_p = cap_tf.paragraphs[0]
                    cap_p.text = caption_text
                    cap_p.alignment = PP_ALIGN.CENTER
                    cap_p.font.size = Pt(12.5)
                    cap_p.font.bold = True
                else:
                    img_top = row_top + (img_avail_h - h) // 2

                # Add picture
                try:
                    slide.shapes.add_picture(BytesIO(img_bytes), img_left, img_top, w, h)
                except Exception as e:
                    _emit_warning(f"[!] Warning: Could not add picture '{local_img}': {e}")

        added_count += 1

    return added_count
