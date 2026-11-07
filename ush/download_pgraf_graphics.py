#!/usr/bin/env python3
"""
download_pgraf_graphics.py: Standalone CLI utility to download pygraf graphics from HPC.

Usage:
    ush/download_pgraf_graphics.py <recipe.yaml> [options]

Options:
    -o, --output-dir DIR   Local destination directory (default: ./graphics)
    -w, --workers N        Max parallel SCP downloads (default: 4)
    --update               Re-download existing files instead of using local cache
    --dry-run              List all targeted remote files without downloading
    --debug                Print verbose transfer details, including missing or uncopied files
    -h, --help             Show this help message and exit
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import os
from pathlib import Path
import re
import shutil
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
if venv_python.is_file() and Path(sys.prefix).resolve() != venv_dir.resolve():
    try:
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve())] + sys.argv[1:])
    except (PermissionError, OSError):
        pass


def find_hpc_config() -> Optional[Path]:
    """Finds .hpc_config in CWD or AutoMATS root directory."""
    cwd_dot = Path.cwd() / ".hpc_config"
    if cwd_dot.exists():
        return cwd_dot
    root_dot = ROOT / ".hpc_config"
    if root_dot.exists():
        return root_dot
    return None


def load_hpc_config() -> Dict[str, Dict[str, Any]]:
    """Loads .hpc_config dictionary."""
    path = find_hpc_config()
    if not path or not path.exists():
        return {}
    cfg: Dict[str, Dict[str, Any]] = {}
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith(":") and ":" not in line[:-1]:
            current = line[:-1].strip().lower()
            cfg[current] = {}
        elif current and ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip()
            if v.lower() in ("null", "none", "~", ""):
                cfg[current][k] = None
            elif v.isdigit():
                cfg[current][k] = int(v)
            else:
                cfg[current][k] = v
    return cfg


def test_local_access(username: str, local_port: int, timeout: float = 2.5) -> bool:
    """Verifies that SSH access through localhost:<local_port> is currently active."""
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
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc.returncode == 0
    except Exception:
        return False


def load_recipe_yaml(path: Path) -> Dict[str, Any]:
    """Loads recipe YAML file using PyYAML or a fallback parser."""
    try:
        import yaml
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except ImportError:
        pass

    # Simple fallback parser for basic key-value / lists / mappings
    raw = path.read_text(encoding="utf-8")
    data: Dict[str, Any] = {}
    current_list_key = None
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        if line.startswith("- ") and current_list_key:
            item_text = line[2:].strip()
            if ":" in item_text:
                sub_k, sub_v = item_text.split(":", 1)
                sub_k = sub_k.strip().strip("'\"")
                sub_v = sub_v.strip()
                if sub_v.startswith("[") and sub_v.endswith("]"):
                    inner = sub_v[1:-1].strip()
                    sub_v = [x.strip() for x in inner.split(",") if x.strip()] if inner else []
                data[current_list_key].append({sub_k: sub_v})
            else:
                data[current_list_key].append(item_text)
        elif ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip()
            if not v:
                data[k] = []
                current_list_key = k
            elif v.startswith("[") and v.endswith("]"):
                inner = v[1:-1].strip()
                items = [x.strip() for x in inner.split(",") if x.strip()] if inner else []
                data[k] = items
                current_list_key = None
            else:
                data[k] = v
                current_list_key = None
    return data


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
    port: int,
    tasks: List[Tuple[str, Path]],
    cluster: str = "HPC",
    debug: bool = False,
) -> Tuple[int, int, int]:
    """
    Downloads remote files using a single streaming tar session over SSH.
    - Sends all remote file paths to remote `tar -P -cf - --null -T -` via stdin.
    - Reads the incoming byte stream locally via `tarfile.open(mode='r|*')`.
    - Directly writes each member file to its target destination folder.
    - Missing files are ignored by remote tar and recorded as missing locally.
    Returns (completed_count, missing_count, error_count).
    """
    if not tasks:
        return 0, 0, 0

    path_map: Dict[str, Path] = {r.lstrip("/"): l for r, l in tasks}
    norm_to_remote: Dict[str, str] = {r.lstrip("/"): r for r, _ in tasks}
    received_remote_paths: set = set()
    total_tasks = len(tasks)

    # Pack paths separated by null byte \0 for tar --null -T -
    path_data = ("\0".join(r for r, _ in tasks) + "\0").encode("utf-8")

    cmd = [
        "ssh",
        "-p", str(port),
        "-o", "BatchMode=yes",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "StrictHostKeyChecking=no",
        "-o", "LogLevel=ERROR",
        f"{username}@localhost",
        "tar -P -cf - --null -T -",
    ]

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

    try:
        if proc.stdout:
            with tarfile.open(fileobj=proc.stdout, mode="r|*") as tar_stream:
                for member in tar_stream:
                    if not member.isfile():
                        continue
                    norm = member.name.lstrip("/")
                    dest = path_map.get(norm)
                    if not dest:
                        continue

                    dest.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        fin = tar_stream.extractfile(member)
                        if fin:
                            with dest.open("wb") as fout:
                                shutil.copyfileobj(fin, fout, length=64 * 1024)
                            if dest.exists() and dest.stat().st_size > 0:
                                completed += 1
                                orig_r = norm_to_remote.get(norm, norm)
                                received_remote_paths.add(orig_r)
                            elif dest.exists() and dest.stat().st_size == 0:
                                dest.unlink(missing_ok=True)
                    except Exception as e:
                        errors += 1
                        if debug:
                            print(f"  [DEBUG] Error writing {dest}: {e}", file=sys.stderr)

                    total_done = completed + errors
                    if total_done % 10 == 0 or total_done == total_tasks:
                        pct = int(100 * total_done / total_tasks)
                        print(f"  Progress: {total_done}/{total_tasks} ({pct}%) [{completed} ok]", flush=True)

    except tarfile.ReadError:
        # Happens if stream is empty (i.e. all files were missing on remote)
        pass
    except (subprocess.SubprocessError, OSError) as e:
        if completed == 0:
            errors += 1
            print(f"[!] Error reading tar stream: {e}", file=sys.stderr)
    finally:
        t_in.join(timeout=3.0)
        t_err.join(timeout=3.0)
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    missing_tasks = [(r, l) for r, l in tasks if r not in received_remote_paths]
    missing = len(missing_tasks)

    # In debug mode, list missing files
    if debug and missing > 0:
        for r_path, _ in missing_tasks:
            print(f"  [DEBUG] Missing on {cluster}: {Path(r_path).name}\n          Remote path: {r_path}", file=sys.stderr)

    if debug and stderr_lines:
        unexpected = [
            line for line in stderr_lines
            if "Cannot stat: No such file or directory" not in line
            and "Error exit delayed" not in line
            and "Removing leading" not in line
        ]
        if unexpected:
            print(f"\n[DEBUG] Remote stderr output:", file=sys.stderr)
            for u in unexpected[:10]:
                print(f"  {u}", file=sys.stderr)

    return completed, missing, errors


def build_download_list(recipe: Dict[str, Any], output_base: Path) -> Tuple[str, str, int, str, List[Tuple[str, Path]]]:
    """
    Builds the list of (remote_path, local_path) tuples from recipe configuration.
    Returns (hpc_cluster, username, port, expname, tasks).
    """
    comroot_raw = str(recipe.get("COMROOT", "")).strip()
    if not comroot_raw:
        raise ValueError("Missing 'COMROOT' in recipe.")

    if ":" in comroot_raw:
        hpc_cluster, remote_comroot = comroot_raw.split(":", 1)
        hpc_cluster = hpc_cluster.strip().lower()
        remote_comroot = remote_comroot.strip()
    else:
        hpc_cluster = "localhost"
        remote_comroot = comroot_raw

    expname = extract_expname(remote_comroot)
    prefix = str(recipe.get("prefix", "rrfs")).strip()

    # WGF list
    raw_wgf = recipe.get("WGF", ["det"])
    wgf_list = raw_wgf if isinstance(raw_wgf, list) else [raw_wgf]
    wgf_list = [str(x).strip() for x in wgf_list if str(x).strip()]

    # Tile list
    raw_tile = recipe.get("tile", ["full"])
    tile_list = raw_tile if isinstance(raw_tile, list) else [raw_tile]
    tile_list = [str(x).strip() for x in tile_list if str(x).strip()]

    # Cycles and Fhrs
    cycles = parse_cycle_list(recipe.get("cycle", []))
    fhr_cfg = parse_fhr_config(recipe.get("fhr", [0]))

    # Templates
    templates = (
        recipe.get("graphics name template")
        or recipe.get("graphics_name_template")
        or []
    )
    if isinstance(templates, str):
        templates = [templates]
    templates = [str(t).strip() for t in templates if str(t).strip()]
    if not templates:
        raise ValueError("No 'graphics name template' specified in recipe.")

    # Check HPC config & connection info
    hpc_configs = load_hpc_config()
    c_info = hpc_configs.get(hpc_cluster, {})
    username = c_info.get("username", "")
    port = c_info.get("my_local_port", 0)

    tasks: List[Tuple[str, Path]] = []
    clean_comroot = remote_comroot.rstrip("/")

    for cyc_str in cycles:
        pdy = cyc_str[:8]
        cyc = cyc_str[8:10]
        fhrs = resolve_fhrs_for_cycle(fhr_cfg, cyc_str)
        for wgf in wgf_list:
            for tile in tile_list:
                for fhr in fhrs:
                    valid_time = f"{pdy}{cyc}{fhr:02d}"
                    fhr_3d = f"{fhr:03d}"
                    for tmpl in templates:
                        # Normalize .pg to .png
                        t_norm = re.sub(r"\.pg$", ".png", tmpl, flags=re.IGNORECASE)
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

    return hpc_cluster, username, port, expname, tasks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download selected pygraf graphics from NOAA RDHPCS over SSH tunnel.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "recipe",
        type=Path,
        help="Path to graphics download recipe YAML (e.g. recipes/download_pgraf_graphics.yaml)",
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()

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
        cluster, username, port, expname, tasks = build_download_list(recipe, args.output_dir)
    except Exception as e:
        print(f"Error parsing recipe: {e}", file=sys.stderr)
        return 1

    print("=" * 70)
    print(f"[*] PYGRAF GRAPHICS DOWNLOAD: {expname}")
    print(f"[*] Cluster:       {cluster}")
    print(f"[*] Recipe:        {recipe_path.name}")
    print(f"[*] Local Target:  {args.output_dir / expname}")
    print(f"[*] Total Targets: {len(tasks)} files")
    if args.debug:
        print(f"[*] Debug Mode:    ENABLED")
    print("=" * 70)

    if args.debug:
        fhr_cfg = parse_fhr_config(recipe.get("fhr", [0]))
        print("\n[DEBUG] Forecast lead hours (fhr) breakdown:")
        if fhr_cfg.get("legacy") is not None:
            print(f"  - All cycles: {len(fhr_cfg['legacy'])} leads: {fhr_cfg['legacy']}")
        else:
            for h_set, f_list in fhr_cfg.get("specific", []):
                h_str = ",".join(f"{h:02d}" for h in sorted(h_set))
                print(f"  - Cycles [{h_str}]: {len(f_list)} lead(s) (F{f_list[0]:03d}-F{f_list[-1]:03d})")
            if fhr_cfg.get("default") is not None:
                def_list = fhr_cfg["default"]
                print(f"  - Default cycles: {len(def_list)} lead(s) (F{def_list[0]:03d}-F{def_list[-1]:03d})")

    if args.dry_run:
        limit = len(tasks) if args.debug else 20
        print("\n[DRY RUN] Planned remote -> local file targets:")
        for r_path, l_path in tasks[:limit]:
            print(f"  {r_path}\n    -> {l_path}")
        if len(tasks) > limit:
            print(f"  ... and {len(tasks) - limit} more files (use --debug to display all).")
        print(f"\n[✓] Dry run completed for {len(tasks)} files.")
        return 0

    # Verify cluster connection
    if not username or not port:
        print(f"\n[!] Error: Cluster '{cluster}' is not configured in .hpc_config.", file=sys.stderr)
        return 1

    if args.debug:
        print(f"\n[*] [DEBUG] Verifying SSH tunnel access: {username}@localhost:{port}...")

    if not test_local_access(username, port):
        print("\n" + "=" * 70, file=sys.stderr)
        print(f"[!] Local SSH connection to '{cluster}' is not ready (localhost:{port}).", file=sys.stderr)
        print("[!] Please establish the SSH tunnel first in your terminal:", file=sys.stderr)
        print(f"        ush/ssh_helper.py {cluster}", file=sys.stderr)
        print("    and then rerun.", file=sys.stderr)
        print("=" * 70 + "\n", file=sys.stderr)
        return 1

    # Filter out already existing files unless --update
    pending_tasks = []
    already_cached = 0
    for r_path, l_path in tasks:
        if not args.update and l_path.exists() and l_path.stat().st_size > 0:
            already_cached += 1
        else:
            pending_tasks.append((r_path, l_path))

    if already_cached > 0:
        print(f"[*] Local cache: {already_cached} file(s) already exist (skipped).")

    if not pending_tasks:
        print(f"[✓] All {len(tasks)} files are already downloaded and cached locally!")
        return 0

    print(f"[*] Starting download of {len(pending_tasks)} file(s) via streaming tar over SSH...\n")

    completed = 0
    missing = 0
    failed = 0
    t0 = time.time()

    try:
        completed, missing, failed = stream_tar_pull(
            username=username,
            port=port,
            tasks=pending_tasks,
            cluster=cluster,
            debug=args.debug,
        )
    finally:
        # Remove all empty directories if no files downloaded in them
        removed_count = cleanup_empty_directories(args.output_dir / expname)
        if args.debug and removed_count > 0:
            print(f"\n[*] [DEBUG] Cleaned up {removed_count} empty directory(ies).")
        try:
            if args.output_dir.exists() and not any(args.output_dir.iterdir()):
                args.output_dir.rmdir()
                if args.debug:
                    print(f"[*] [DEBUG] Removed base directory {args.output_dir} as it is empty.")
        except OSError:
            pass

    elapsed = time.time() - t0
    target_exp_dir = args.output_dir / expname
    print("\n" + "=" * 70)
    print(f"[✓] Completed in {elapsed:.1f}s")
    print(f"    - Downloaded:  {completed}")
    print(f"    - Already had: {already_cached}")
    if args.debug and missing > 0:
        print(f"    - Missing:     {missing}")
    if failed > 0:
        print(f"    - Errors:      {failed}")
    if target_exp_dir.exists():
        print(f"    - Directory:   {target_exp_dir}")
    else:
        print(f"    - Directory:   (no files downloaded; empty directory removed)")
    print("=" * 70)

    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
