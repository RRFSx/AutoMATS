"""
pygraf graphics slides module for AutoMATS.

Handles:
  1. Parsing pygraf graphics layout recipes (same valid time, same cycle).
  2. Resolving experiments and COMROOT locations on HPC systems (e.g. gaeac6).
  3. Verifying local SSH tunnel readiness; skipping and prompting users if offline.
  4. Pulling remote graphic files via SCP over local forwarded ports.
  5. Generating OBS and missing-image placeholders.
  6. Rendering graphic slides into the presentation deck.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    yaml = None

from automats.config import Experiment, Plan

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


def find_hpc_config_file() -> Optional[Path]:
    """Finds .hpc_config path in CWD or AutoMATS repository root."""
    cwd_dot = Path.cwd() / ".hpc_config"
    if cwd_dot.exists():
        return cwd_dot
    script_dir = Path(__file__).resolve().parent
    automats_root = script_dir.parent.parent
    root_dot = automats_root / ".hpc_config"
    if root_dot.exists():
        return root_dot
    return None


def load_hpc_config_dict() -> Dict[str, Dict[str, Any]]:
    """Loads .hpc_config without external dependencies."""
    path = find_hpc_config_file()
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


def test_hpc_local_access(username: str, local_port: int, timeout: float = 2.5) -> bool:
    """Tests whether SSH access through localhost:<local_port> is currently active."""
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


def resolve_experiment(ref: str, experiments: tuple[Experiment, ...]) -> Optional[Experiment]:
    """Resolves an experiment reference ('first', 'second', experiment name/label)."""
    norm = ref.strip().lower()
    if norm in ORDINALS:
        idx = ORDINALS[norm]
        if idx < len(experiments):
            return experiments[idx]
        return None
    for exp in experiments:
        if exp.name.lower() == norm or exp.label.lower() == norm:
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

    m_fhr = re.search(r"_f(\d+)\.png$", clean_name, re.IGNORECASE)
    fhr = int(m_fhr.group(1)) if m_fhr else 0

    parts = clean_name.split("_")
    tile = parts[1] if len(parts) >= 3 else "full"
    return clean_name, tile, fhr


def compute_valid_time(cycle: str | int, fhr: int) -> Tuple[str, str, str]:
    """
    Given cycle (e.g. 2024051300) and forecast hour (e.g. 6):
    Returns (pdy, cyc, valid_time_dir).
    valid_time_dir is YYYYMMDDxxxx where xxxx is 4-digit forecast lead hour (e.g. 202405130006).
    """
    c_str = str(cycle).strip()
    if len(c_str) < 10:
        raise ValueError(f"Cycle must be at least 10 characters (YYYYMMDDHH), got: {c_str}")
    pdy = c_str[:8]
    cyc = c_str[8:10]
    valid_time_dir = f"{pdy}{fhr:04d}"
    return pdy, cyc, valid_time_dir


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


def scp_pull_file(username: str, port: int, remote_path: str, local_path: Path) -> bool:
    """Pulls a single remote file via SCP over the forwarded SSH port."""
    if local_path.exists() and local_path.stat().st_size > 0:
        return True
    local_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "scp",
        "-P", str(port),
        "-o", "BatchMode=yes",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "StrictHostKeyChecking=no",
        "-o", "LogLevel=ERROR",
        f"{username}@localhost:{remote_path}",
        str(local_path),
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        return proc.returncode == 0 and local_path.exists() and local_path.stat().st_size > 0
    except Exception:
        return False


def batch_pull_graphics_files(tasks: List[Dict[str, Any]], max_workers: int = 4) -> Dict[str, bool]:
    """
    Downloads uncached graphic files concurrently using a ThreadPoolExecutor.
    Maintains a safe worker limit (default: 4) to prevent exceeding sshd connection limits.
    """
    results: Dict[str, bool] = {}
    pending: List[Dict[str, Any]] = []

    for t in tasks:
        loc = t["local_path"]
        if loc.exists() and loc.stat().st_size > 0:
            results[str(loc)] = True
        else:
            pending.append(t)

    if not pending:
        return results

    # Deduplicate pending tasks by (port, remote_path, local_path)
    unique_pending: Dict[Tuple[int, str, str], Dict[str, Any]] = {}
    for t in pending:
        key = (t["port"], t["remote_path"], str(t["local_path"]))
        unique_pending[key] = t

    workers = min(max_workers, len(unique_pending))
    print(f"[*] Downloading {len(unique_pending)} graphics figure(s) via SCP (parallel workers: {workers})...", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(
                scp_pull_file,
                t["username"],
                t["port"],
                t["remote_path"],
                t["local_path"],
            ): t
            for t in unique_pending.values()
        }
        for future in as_completed(future_map):
            task = future_map[future]
            loc_str = str(task["local_path"])
            try:
                ok = future.result()
                results[loc_str] = ok
                if not ok:
                    print(f"[!] Warning: Remote file not found on {task['cluster']}: {task['remote_path']}", file=sys.stderr)
            except Exception as e:
                results[loc_str] = False
                print(f"[!] Warning: Error retrieving {task['remote_path']}: {e}", file=sys.stderr)

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


def process_graphics_slides(
    plan: Plan,
    output_dir: Path,
    deck: Any,
) -> int:
    """
    Checks HPC readiness, downloads necessary graphics files from HPC,
    and appends graphics slides to the PowerPoint presentation deck.
    Returns the number of graphics slides successfully added.
    """
    if not plan.pygraf_graphics or not plan.pygraf_graphics.enable:
        return 0

    recipe_name = plan.pygraf_graphics.recipe
    if not recipe_name:
        print("[!] Warning: 'pygraf graphics slides' enabled but no 'recipe' file specified.", file=sys.stderr)
        return 0

    recipe_file = resolve_graphics_recipe_path(recipe_name, plan.recipe_path)
    if not recipe_file or not recipe_file.exists():
        print(f"[!] Warning: Graphics recipe file '{recipe_name}' not found.", file=sys.stderr)
        return 0

    if yaml is None:
        print("[!] Warning: PyYAML is required to read graphics recipe; activate the virtual environment.", file=sys.stderr)
        return 0

    try:
        with recipe_file.open(encoding="utf-8") as f:
            raw_data = yaml.safe_load(f)
    except Exception as e:
        print(f"[!] Warning: Failed to parse graphics recipe '{recipe_file}': {e}", file=sys.stderr)
        return 0

    slides_config = raw_data.get("graphics slides") or raw_data.get("graphics_slides")
    if not slides_config or not isinstance(slides_config, list):
        print(f"[!] Warning: No 'graphics slides' list found in '{recipe_file}'.", file=sys.stderr)
        return 0

    # 1. Identify all required HPC clusters from experiments
    hpc_cfg = load_hpc_config_dict()
    needed_clusters: Dict[str, Tuple[str, int]] = {}
    for exp in plan.experiments:
        cluster = exp.hpc_cluster
        if cluster and cluster not in needed_clusters:
            c_info = hpc_cfg.get(cluster, {})
            user = c_info.get("username")
            port = c_info.get("my_local_port")
            if user and port:
                needed_clusters[cluster] = (user, port)
            else:
                needed_clusters[cluster] = ("", 0)

    # 2. Check if local connections to needed HPC clusters are active
    unready_clusters = []
    for cluster, (user, port) in needed_clusters.items():
        if not user or not port:
            unready_clusters.append((cluster, port))
            continue
        if not test_hpc_local_access(user, port):
            unready_clusters.append((cluster, port))

    if unready_clusters:
        print("\n" + "=" * 70, file=sys.stderr)
        for cluster, port in unready_clusters:
            port_str = f"localhost:{port}" if port else "not configured"
            print(f"[!] Local SSH connection to '{cluster}' is not ready ({port_str}).", file=sys.stderr)
        print("[!] Skipping pygraf graphics slides.", file=sys.stderr)
        print("[!] To enable graphics slides, please establish the SSH tunnel first:", file=sys.stderr)
        for cluster, _ in unready_clusters:
            print(f"        ush/ssh_helper.py {cluster}", file=sys.stderr)
        print("    and then rerun.", file=sys.stderr)
        print("=" * 70 + "\n", file=sys.stderr)
        return 0

    # 3. Phase 1: Collect and batch-pull all needed graphic figures in parallel
    transfer_tasks: List[Dict[str, Any]] = []
    for spec in slides_config:
        slide_type = str(spec.get("type", "same valid time")).strip().lower().replace("_", " ")
        prefix = spec.get("prefix", "rrfs")
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
                if item_str.lower() == "obs":
                    continue
                if "valid" in slide_type:
                    exp_obj = resolve_experiment(item_str, plan.experiments)
                    g_name = spec.get("graphics name", "cref_full_sfc_f006.png")
                    clean_name, tile_extracted, fhr = parse_graphics_name(g_name)
                    tile = tile_override or tile_extracted
                    if exp_obj and exp_obj.hpc_cluster and exp_obj.remote_comroot:
                        cluster = exp_obj.hpc_cluster
                        user, port = needed_clusters.get(cluster, ("", 0))
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
                            "port": port,
                            "remote_path": rem_path,
                            "local_path": local_img,
                            "cluster": cluster,
                            "clean_name": clean_name,
                        })
                else:
                    exp_ref = spec.get("experiment", "first")
                    exp_obj = resolve_experiment(exp_ref, plan.experiments)
                    clean_name, tile_extracted, fhr = parse_graphics_name(item_str)
                    tile = tile_override or tile_extracted
                    if exp_obj and exp_obj.hpc_cluster and exp_obj.remote_comroot:
                        cluster = exp_obj.hpc_cluster
                        user, port = needed_clusters.get(cluster, ("", 0))
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
                            "port": port,
                            "remote_path": rem_path,
                            "local_path": local_img,
                            "cluster": cluster,
                            "clean_name": clean_name,
                        })

    if transfer_tasks:
        batch_pull_graphics_files(transfer_tasks, max_workers=4)

    # 4. Phase 2: Render each graphics slide into PowerPoint deck
    from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
    from pptx.util import Inches, Pt
    from PIL import Image

    margin = Inches(0.4)
    content_top = Inches(0.85)
    content_bottom = Inches(7.1)
    avail_height = content_bottom - content_top
    caption_height = Inches(0.3)

    added_count = 0

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

        # Title text
        if "valid" in slide_type:
            g_name = spec.get("graphics name", "")
            clean_name, tile, fhr = parse_graphics_name(g_name)
            title_text = f"{title} - Cycle: {cycle} F{fhr:03d}"
        else:
            exp_ref = spec.get("experiment", "first")
            exp_obj = resolve_experiment(exp_ref, plan.experiments)
            exp_name = exp_obj.name if exp_obj else exp_ref
            title_text = f"{title} - {exp_name} (Cycle: {cycle})"

        heading = slide.shapes.add_textbox(margin, Inches(0.18), deck.slide_width - 2 * margin, Inches(0.55))
        heading.text_frame.word_wrap = True
        heading.text_frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
        p = heading.text_frame.paragraphs[0]
        p.text = title_text
        p.alignment = PP_ALIGN.CENTER
        p.font.size = Pt(22)
        p.font.bold = True

        row_gap = Inches(0.15)
        row_height = (avail_height - row_gap * (len(rows_data) - 1)) // len(rows_data)

        for row_idx, row_items in enumerate(rows_data):
            row_top = content_top + row_idx * (row_height + row_gap)
            col_gap = Inches(0.15)
            cell_width = (deck.slide_width - 2 * margin - col_gap * (len(row_items) - 1)) // len(row_items)
            img_avail_h = row_height - caption_height - Inches(0.04)

            for col_idx, item in enumerate(row_items):
                item_str = str(item).strip()
                cell_left = margin + col_idx * (cell_width + col_gap)

                if item_str.lower() == "obs":
                    caption_text = "MRMS OBS"
                    local_img = get_obs_placeholder(output_dir)
                elif "valid" in slide_type:
                    # 'same valid time': item is experiment reference (e.g. 'first', 'second')
                    exp_obj = resolve_experiment(item_str, plan.experiments)
                    caption_text = exp_obj.name if exp_obj else item_str
                    g_name = spec.get("graphics name", "cref_full_sfc_f006.png")
                    clean_name, tile_extracted, fhr = parse_graphics_name(g_name)

                    if exp_obj and exp_obj.hpc_cluster and exp_obj.remote_comroot:
                        local_img = output_dir / "graphics" / exp_obj.name / f"{cycle}_{fhr:03d}_{clean_name}"
                        if not (local_img.exists() and local_img.stat().st_size > 0):
                            local_img = get_missing_placeholder(output_dir, clean_name)
                    else:
                        if not exp_obj:
                            print(f"[!] Warning: Experiment reference '{item_str}' could not be resolved.", file=sys.stderr)
                        elif not exp_obj.COMROOT:
                            print(f"[!] Warning: Experiment '{exp_obj.name}' has no COMROOT configured.", file=sys.stderr)
                        local_img = get_missing_placeholder(output_dir, clean_name)
                else:
                    # 'same cycle': item is graphics filename (e.g. 'cref_full_sfc_f001.png')
                    exp_ref = spec.get("experiment", "first")
                    exp_obj = resolve_experiment(exp_ref, plan.experiments)
                    clean_name, tile_extracted, fhr = parse_graphics_name(item_str)
                    caption_text = f"F{fhr:03d}"

                    if exp_obj and exp_obj.hpc_cluster and exp_obj.remote_comroot:
                        local_img = output_dir / "graphics" / exp_obj.name / f"{cycle}_{fhr:03d}_{clean_name}"
                        if not (local_img.exists() and local_img.stat().st_size > 0):
                            local_img = get_missing_placeholder(output_dir, clean_name)
                    else:
                        if not exp_obj:
                            print(f"[!] Warning: Experiment reference '{item_str}' could not be resolved.", file=sys.stderr)
                        elif not exp_obj.COMROOT:
                            print(f"[!] Warning: Experiment '{exp_obj.name}' has no COMROOT configured.", file=sys.stderr)
                        local_img = get_missing_placeholder(output_dir, clean_name)

                # Measure and position image
                try:
                    with Image.open(local_img) as im:
                        pw, ph = im.size
                except Exception:
                    pw, ph = 1200, 900

                scale = min(cell_width / pw, img_avail_h / ph)
                w = max(1, int(pw * scale))
                h = max(1, int(ph * scale))
                img_left = cell_left + (cell_width - w) // 2
                img_top = row_top + caption_height + (img_avail_h - h) // 2

                # Add caption centered above the figure
                cap_box = slide.shapes.add_textbox(cell_left, row_top, cell_width, caption_height)
                cap_tf = cap_box.text_frame
                cap_tf.word_wrap = True
                cap_tf.margin_left = cap_tf.margin_right = cap_tf.margin_top = cap_tf.margin_bottom = 0
                cap_p = cap_tf.paragraphs[0]
                cap_p.text = caption_text
                cap_p.alignment = PP_ALIGN.CENTER
                cap_p.font.size = Pt(13)
                cap_p.font.bold = True

                # Add picture
                try:
                    img_data = local_img.read_bytes()
                    slide.shapes.add_picture(BytesIO(img_data), img_left, img_top, w, h)
                except Exception as e:
                    print(f"[!] Warning: Could not add picture '{local_img}': {e}", file=sys.stderr)

        added_count += 1

    return added_count
