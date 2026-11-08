"""
MRMS radar observation fetcher for AutoMATS.

Retrieves observed composite reflectivity (and other variable) maps with legends
from NOAA NSSL MRMS Product Viewer using browser automation, mirroring Verification Studio.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
except ImportError:
    sync_playwright = None
    BrowserTimeout = Exception

# Standard user agent and anti-automation flags aligned with AutoMATS runner
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
CHROMIUM_ARGS = ["--disable-blink-features=AutomationControlled"]
CHROMIUM_IGNORE_DEFAULT_ARGS = ["--enable-automation"]

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_END = b"\x00\x00\x00\x00IEND\xaeB\x60\x82"

# Product mapping: variable -> (product_type, product)
MRMS_PRODUCTS: Dict[str, Tuple[str, str]] = {
    "cref": ("crefls", "CREF"),
    "ref": ("crefls", "CREF"),
    "echo_top_18": ("echotopls", "EchoTop_18"),
    "echo_top_50": ("echotopls", "EchoTop_50"),
}

BOUNDING_BOX_SCRIPT = """(() => {
    const baseMap = document.getElementById('base_map');
    const sideLegend = document.getElementById('side_legend');
    if (!baseMap || !sideLegend) return JSON.stringify(null);
    const bBox = baseMap.getBoundingClientRect();
    const lBox = sideLegend.getBoundingClientRect();
    const x = bBox.left;
    const y = bBox.top;
    const w = (lBox.left + lBox.width) - x;
    const h = Math.max(bBox.height, lBox.height) + 3;
    return JSON.stringify({ x: x, y: y, width: w, height: h });
})()"""


def format_mrms_filename(var: str, valid_dt: datetime) -> str:
    """
    Formats the MRMS filename based strictly on variable and valid observation time.
    Format: mrms_{var}_{YYYYMMDDHHMM}.png (e.g. mrms_cref_202405070600.png)
    """
    clean_var = re.sub(r"[^A-Za-z0-9_-]+", "", var.strip().lower()) or "cref"
    time_str = valid_dt.strftime("%Y%m%d%H%M")
    return f"mrms_{clean_var}_{time_str}.png"


def parse_mrms_filename(filename: str) -> Optional[Tuple[str, datetime]]:
    """
    Parses 'mrms_{var}_{YYYYMMDDHHMM}.png' (or 10-digit 'mrms_{var}_{YYYYMMDDHH}.png').
    Returns (var, valid_datetime) or None.
    """
    name = Path(filename).name
    m = re.fullmatch(r"mrms_([a-zA-Z0-9_-]+)_(\d{10}|\d{12})\.png", name, re.IGNORECASE)
    if not m:
        return None
    var = m.group(1).lower()
    t_str = m.group(2)
    fmt = "%Y%m%d%H%M" if len(t_str) == 12 else "%Y%m%d%H"
    try:
        dt = datetime.strptime(t_str, fmt)
        return var, dt
    except ValueError:
        return None


def is_valid_mrms_file(path: Path) -> bool:
    """Verifies that the target file exists, is non-empty (>1000 bytes), and is a valid PNG."""
    try:
        if not path.is_file() or path.stat().st_size <= 1000:
            return False
        with path.open("rb") as stream:
            header = stream.read(len(PNG_SIGNATURE))
            if header != PNG_SIGNATURE:
                return False
            stream.seek(-len(PNG_END), os.SEEK_END)
            return stream.read(len(PNG_END)) == PNG_END
    except OSError:
        return False


def build_mrms_url(
    valid_dt: datetime,
    var: str = "cref",
    zoom: int = 4,
    clon: int = -98,
    clat: int = 38,
) -> str:
    """Constructs the NOAA NSSL MRMS Product Viewer URL for a given observation valid datetime."""
    norm_var = var.strip().lower()
    ptype, prod = MRMS_PRODUCTS.get(norm_var, (f"{norm_var}ls", norm_var.upper()))
    return (
        f"https://mrms.nssl.noaa.gov/qvs/product_viewer/index.php?"
        f"web_exec_mode=run&menu=menu_config.txt&year={valid_dt.year}&month={valid_dt.month}"
        f"&day={valid_dt.day}&hour={valid_dt.hour}&minute={valid_dt.minute}"
        f"&time_mode=static&zoom={zoom}&clon={clon}&clat={clat}&base=0&overlays=1&mping_mode=0"
        f"&product_type={ptype}&product={prod}"
    )


def fetch_single_mrms(
    page: Any,
    url: str,
    target_path: Path,
    max_attempts: int = 3,
    retry_delays: Tuple[float, ...] = (10.0, 20.0),
    log: Callable[[str], None] = print,
) -> bool:
    """
    Fetches a single MRMS observation image using an active Playwright page.
    Waits for #base_map, settles 2.5s, evaluates bounding box, captures clipped screenshot,
    and atomically saves to target_path. Retries on failure up to max_attempts.
    """
    mrms_fn = target_path.name
    last_err: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        try:
            if attempt > 1:
                log(f"  * Retrying MRMS for {mrms_fn} (attempt {attempt}/{max_attempts})...")

            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("#base_map", timeout=30000)

            # Settle wait for radar tiles and overlay canvases (2.5s, matching Verification Studio)
            page.wait_for_timeout(2500)

            box_json = page.evaluate(BOUNDING_BOX_SCRIPT)
            if not box_json:
                raise RuntimeError("Could not evaluate MRMS map/legend bounding box")

            box = json.loads(box_json)
            if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
                raise RuntimeError("Invalid bounding box for MRMS map/legend")

            clip = {
                "x": float(box["x"]),
                "y": float(box["y"]),
                "width": float(box["width"]),
                "height": float(box["height"]),
            }

            buf = page.screenshot(clip=clip)
            if len(buf) <= 1000:
                raise RuntimeError(f"Screenshot buffer too small ({len(buf)} bytes)")

            target_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = target_path.with_suffix(f".tmp.{os.getpid()}.{int(time.time() * 1000)}")
            tmp_path.write_bytes(buf)
            tmp_path.replace(target_path)

            log(f"    [CACHE MISS] Downloaded fresh MRMS observation: {mrms_fn} ({len(buf)} bytes)")
            return True
        except Exception as e:
            last_err = e
            if attempt < max_attempts:
                delay = retry_delays[min(attempt - 1, len(retry_delays) - 1)]
                log(f"    [MRMS RETRY {attempt}/{max_attempts}] Attempt {attempt} failed for {mrms_fn}: {e}. Retrying in {delay}s...")
                time.sleep(delay)

    log(f"    [MRMS ERROR] Failed fetching MRMS {mrms_fn} after {max_attempts} attempts: {last_err}")
    return False


def fetch_mrms_images(
    tasks: List[Dict[str, Any]],
    output_dir: Path,
    force_fetch: bool = False,
    headless: bool = True,
    show_browser: bool = False,
    log: Callable[[str], None] = print,
    on_progress: Optional[Callable[[int, str], None]] = None,
) -> Dict[str, Path]:
    """
    Batch fetches uncached MRMS observation images using a single browser session.

    Each task in tasks should be a dict:
        {"var": "cref", "valid_dt": datetime(...), "dest_path": Optional[Path]}

    Returns mapping of dest_path string to Path.
    """
    if not tasks:
        if on_progress:
            on_progress(100, "Done")
        return {}

    obs_dir = output_dir / "graphics" / "obs"
    obs_dir.mkdir(parents=True, exist_ok=True)

    results: Dict[str, Path] = {}
    needed: List[Dict[str, Any]] = []

    # 1. Check local cache
    for t in tasks:
        var = str(t.get("var", "cref")).strip().lower()
        valid_dt = t["valid_dt"]
        dest = t.get("dest_path")
        if not dest:
            dest = obs_dir / format_mrms_filename(var, valid_dt)
        dest_path = Path(dest)
        results[str(dest_path)] = dest_path

        if not force_fetch and is_valid_mrms_file(dest_path):
            log(f"  [CACHE HIT] Using cached MRMS: {dest_path.name}")
        else:
            needed.append({
                "var": var,
                "valid_dt": valid_dt,
                "dest_path": dest_path,
                "url": build_mrms_url(valid_dt, var),
            })

    if not needed:
        log("  [ALL CACHED] All MRMS observation radar images already available locally.")
        if on_progress:
            on_progress(100, "Done")
        return results

    if sync_playwright is None:
        log("[!] Warning: Playwright is missing; cannot download MRMS observation images from NSSL.")
        return results

    total_needed = len(needed)
    log(f"[*] Launching browser to fetch {total_needed} MRMS observation image(s) from NOAA NSSL...")

    with sync_playwright() as pw:
        run_headless = headless and not show_browser
        browser = None
        # Attempt channel="chrome" first, falling back to default chromium
        try:
            browser = pw.chromium.launch(
                channel="chrome",
                headless=run_headless,
                args=CHROMIUM_ARGS,
                ignore_default_args=CHROMIUM_IGNORE_DEFAULT_ARGS,
            )
        except Exception:
            browser = pw.chromium.launch(
                headless=run_headless,
                args=CHROMIUM_ARGS,
                ignore_default_args=CHROMIUM_IGNORE_DEFAULT_ARGS,
            )

        try:
            context = browser.new_context(
                viewport={"width": 1600, "height": 1400},
                user_agent=DEFAULT_USER_AGENT,
            )
            page = context.new_page()

            for idx, item in enumerate(needed, 1):
                mrms_fn = item["dest_path"].name
                v_str = item["valid_dt"].strftime("%Y-%m-%d %H:%M")
                log(f"  [{idx}/{total_needed}] Fetching MRMS for Valid: {v_str} UTC ({mrms_fn})...")

                if on_progress:
                    pct = int(100 * idx / total_needed)
                    on_progress(pct, f"Fetching {mrms_fn}")

                fetch_single_mrms(
                    page=page,
                    url=item["url"],
                    target_path=item["dest_path"],
                    log=log,
                )
        finally:
            browser.close()

    if on_progress:
        on_progress(100, "Done")

    return results
