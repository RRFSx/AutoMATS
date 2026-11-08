#!/usr/bin/env python3
"""
download_mrms.py: Standalone CLI utility to download MRMS observation plots from NOAA NSSL.

Usage:
    ush/download_mrms.py <recipe.yaml> [options]
    ush/download_mrms.py --time <time_spec> [options]

Options:
    -o, --output-dir DIR   Local destination directory (default: ./graphics/obs)
    --var VAR              Variable / product to retrieve (default: cref)
    --time TIME_SPEC       Observation valid time, list, or range
                           (e.g. 2024050700-2024050800-1h, 202405070600, or 2024050700,2024050706)
    --update               Re-download existing figures instead of using local cache
    --dry-run              List all targeted observation times and URLs without downloading
    --show-browser         Keep browser window visible during download
    -h, --help             Show this help message and exit
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
venv_dir = ROOT / ".venv"
venv_python = venv_dir / "bin" / "python"
if venv_python.is_file() and Path(sys.prefix).resolve() != venv_dir.resolve():
    try:
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve())] + sys.argv[1:])
    except (PermissionError, OSError):
        pass

# Add src to sys.path
src_dir = ROOT / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

try:
    import yaml
except ImportError:
    yaml = None

from automats.mrms import (
    build_mrms_url,
    fetch_mrms_images,
    format_mrms_filename,
    is_valid_mrms_file,
)
from automats.graphics import parse_graphics_name, resolve_obs_cell_time, resolve_obs_tile


def parse_time_spec(spec: str) -> List[datetime]:
    """
    Parses observation time specifications:
      - Range with optional step: '2024050700-2024050800-1h' or '202405070000-202405080000-30m'
      - Range with default 1h step: '2024050700-2024050800'
      - Comma-separated: '2024050700,2024050706,2024050712'
      - Single timestamp: '202405070600' or '2024050706'
    """
    results: List[datetime] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        # Check range pattern: START-END[-STEP]
        m_range = re.fullmatch(r"(\d{10}|\d{12})-(\d{10}|\d{12})(?:-([0-9]+[hmHM]?))?", part)
        if m_range:
            s_str, e_str, step_str = m_range.groups()
            s_fmt = "%Y%m%d%H%M" if len(s_str) == 12 else "%Y%m%d%H"
            e_fmt = "%Y%m%d%H%M" if len(e_str) == 12 else "%Y%m%d%H"
            s_dt = datetime.strptime(s_str, s_fmt)
            e_dt = datetime.strptime(e_str, e_fmt)

            step = timedelta(hours=1)
            if step_str:
                step_str = step_str.lower()
                if step_str.endswith("m"):
                    step = timedelta(minutes=int(step_str[:-1]))
                elif step_str.endswith("h"):
                    step = timedelta(hours=int(step_str[:-1]))
                else:
                    step = timedelta(hours=int(step_str))

            curr = s_dt
            while curr <= e_dt:
                results.append(curr)
                curr += step
        else:
            # Single timestamp
            m_single = re.fullmatch(r"(\d{10}|\d{12})", part)
            if m_single:
                t_str = m_single.group(1)
                fmt = "%Y%m%d%H%M" if len(t_str) == 12 else "%Y%m%d%H"
                results.append(datetime.strptime(t_str, fmt))
            else:
                raise ValueError(
                    f"Invalid observation time specification: '{part}'. "
                    "Expected YYYYMMDDHH, YYYYMMDDHHMM, or START-END-STEP (e.g. 2024050700-2024050800-1h)"
                )
    return sorted(list(set(results)))


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
                    if ":" in content and not content.endswith("]"):
                        k, v = content.split(":", 1)
                        k = k.strip().strip("'\"")
                        v = v.strip()
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
                            if ":" in nl:
                                nk, nv = nl.split(":", 1)
                                nk = nk.strip().strip("'\"")
                                nv = nv.strip()
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
                if ":" in cur_line:
                    k, v = cur_line.split(":", 1)
                    k = k.strip().strip("'\"")
                    v = v.strip()
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


def extract_obs_from_recipe(recipe_path: Path) -> List[Dict[str, Any]]:
    """Reads graphics slides recipe and extracts observation tasks."""
    data = load_recipe_yaml(recipe_path)

    slides_config = data.get("graphics slides") or data.get("graphics_slides")
    if not slides_config or not isinstance(slides_config, list):
        return []

    tasks = []
    for spec in slides_config:
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
                    tasks.append({"var": var, "valid_dt": valid_dt, "tile": tile})
    return tasks


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Download MRMS observation plots from NOAA NSSL Product Viewer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("recipe", nargs="?", type=Path, help="Optional YAML recipe containing graphics slides")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("graphics/obs"), help="Destination directory (default: ./graphics/obs)")
    parser.add_argument("--var", default="cref", help="Variable / product (default: cref)")
    parser.add_argument("--tile", default=None,
                        help="Geographic tile / subdomain (default: full or extracted from recipe; choices: full, NC, NE, NW, SC, SE, SW, EastCO)")
    parser.add_argument("--time", "--valid-time", dest="time",
                        help="Observation valid time, list, or range (e.g. 2024050700-2024050800-1h, 202405070600, or 2024050700,2024050706)")
    parser.add_argument("--update", action="store_true", help="Force re-download even if already cached locally")
    parser.add_argument("--dry-run", action="store_true", help="List targeted observation times and URLs without downloading")
    parser.add_argument("--show-browser", action="store_true", help="Keep browser visible instead of running headless")

    args = parser.parse_args(argv)

    raw_tasks: List[Dict[str, Any]] = []

    # 1. From recipe
    if args.recipe:
        if not args.recipe.is_file():
            print(f"[!] Error: Recipe file not found: {args.recipe}", file=sys.stderr)
            return 1
        try:
            recipe_tasks = extract_obs_from_recipe(args.recipe)
            raw_tasks.extend(recipe_tasks)
            print(f"[*] Loaded {len(recipe_tasks)} observation task(s) from recipe: {args.recipe}")
        except Exception as e:
            print(f"[!] Error reading recipe '{args.recipe}': {e}", file=sys.stderr)
            return 1

    # Override tile from CLI if provided
    if args.tile and raw_tasks:
        for t in raw_tasks:
            t["tile"] = args.tile

    # 2. From --time
    if args.time:
        try:
            parsed_times = parse_time_spec(args.time)
            tile_val = args.tile or "full"
            for v_dt in parsed_times:
                raw_tasks.append({"var": args.var.lower(), "valid_dt": v_dt, "tile": tile_val})
        except Exception as e:
            print(f"[!] Error parsing --time '{args.time}': {e}", file=sys.stderr)
            return 1

    if not raw_tasks:
        print("[!] No observation targets specified. Provide a recipe file or --time <spec>.", file=sys.stderr)
        parser.print_help()
        return 1

    # Deduplicate targets by (var, tile, valid_dt)
    unique_tasks: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    dest_dir = args.output_dir
    dest_dir.mkdir(parents=True, exist_ok=True)

    for t in raw_tasks:
        var = t["var"]
        v_dt = t["valid_dt"]
        tile = t.get("tile", "full")
        fn = format_mrms_filename(var, v_dt, tile=tile)
        dest_path = dest_dir / fn
        key = (var, tile.lower(), v_dt.strftime("%Y%m%d%H%M"))
        if key not in unique_tasks:
            unique_tasks[key] = {
                "var": var,
                "valid_dt": v_dt,
                "tile": tile,
                "dest_path": dest_path,
                "url": build_mrms_url(v_dt, var=var, tile=tile),
            }

    tasks = list(unique_tasks.values())
    print(f"[*] Total unique MRMS observation targets: {len(tasks)}")

    cached_count = sum(1 for t in tasks if not args.update and is_valid_mrms_file(t["dest_path"]))
    needed_count = len(tasks) - cached_count

    print(f"    - Cached: {cached_count}")
    print(f"    - To download: {needed_count}")
    print(f"    - Destination: {dest_dir.resolve()}\n")

    if args.dry_run:
        print("[DRY-RUN] Targeted MRMS observation files:")
        for t in tasks:
            status = "CACHED" if is_valid_mrms_file(t["dest_path"]) and not args.update else "PENDING"
            v_str = t["valid_dt"].strftime("%Y-%m-%d %H:%M UTC")
            tile_label = t.get("tile", "full")
            print(f"  [{status}] {t['dest_path'].name} (Tile: {tile_label}) | Valid: {v_str}")
            print(f"           URL: {t['url']}")
        return 0

    if needed_count == 0:
        print("[*] All requested MRMS observation plots already cached locally.")
        return 0

    try:
        from automats.mrms import fetch_mrms_images
        fetch_mrms_images(
            tasks=tasks,
            output_dir=dest_dir.parent if dest_dir.name == "obs" else dest_dir,
            force_fetch=args.update,
            headless=not args.show_browser,
            show_browser=args.show_browser,
        )
    except Exception as e:
        print(f"[!] Error downloading MRMS images: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
