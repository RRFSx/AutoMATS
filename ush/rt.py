#!/usr/bin/env python3
"""Run real-time AutoMATS comparison: compare rrfsv2x and rrfsv2b against HRRR_OPS."""

import calendar
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import sys
import yaml

# Auto-re-exec with local venv python if invoked by system python or another interpreter
ROOT = Path(__file__).resolve().parent.parent
venv_dir = ROOT / ".venv"
venv_python = venv_dir / "bin" / "python"
if venv_python.is_file() and Path(sys.prefix).resolve() != venv_dir.resolve():
    os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve())] + sys.argv[1:])

sys.path.insert(0, str(ROOT / "src"))

from automats.cli import main as automats_main


def compute_default_dates():
    now = datetime.now(timezone.utc)
    end = (now - timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    month = end.month - 1
    year = end.year
    if month == 0:
        month = 12
        year -= 1
    day = min(end.day, calendar.monthrange(year, month)[1])
    start = end.replace(year=year, month=month, day=day)
    return start, end


def fetch_mats_date_range(base_url="https://gsl.noaa.gov/mats", model="rrfsv2x"):
    """Query MATS for the active latest date range of the model in the MATS database."""
    try:
        from playwright.sync_api import sync_playwright
        from automats.runner import CHROMIUM_ARGS, CHROMIUM_IGNORE_DEFAULT_ARGS, DEFAULT_USER_AGENT
        from automats.browser import UpperAirBrowser

        with sync_playwright() as p:
            browser = p.chromium.launch(
                channel="chrome",
                headless=True,
                args=CHROMIUM_ARGS,
                ignore_default_args=CHROMIUM_IGNORE_DEFAULT_ARGS,
            )
            try:
                context = browser.new_context(
                    viewport={"width": 1600, "height": 1400},
                    user_agent=DEFAULT_USER_AGENT,
                )
                client = UpperAirBrowser(context, base_url)
                client.application = "surface"
                client.expected = {}
                client.page = context.new_page()
                client._open_page(f"{base_url.rstrip('/')}/surface")
                client.page.wait_for_function(
                    '() => typeof matsParamUtils !== "undefined" && (matsParamUtils.getOptionsForParam("data-source") || []).length > 0',
                    timeout=15000,
                )
                client.page.select_option("#plotTypes-selector", value="TimeSeries")
                client.page.wait_for_function('value => Session.get("plotType") === value', arg="TimeSeries", polling=250)
                client._settle_controls()
                client._set("data-source", model)
                dates_str = client.page.evaluate('() => matsParamUtils.getValueForParamName("dates")')
                if dates_str and " - " in dates_str:
                    s_str, e_str = dates_str.split(" - ")
                    s_dt = datetime.strptime(s_str.strip(), "%m/%d/%Y %H:%M").replace(tzinfo=timezone.utc)
                    e_dt = datetime.strptime(e_str.strip(), "%m/%d/%Y %H:%M").replace(tzinfo=timezone.utc)
                    return s_dt, e_dt
            finally:
                browser.close()
    except Exception:
        pass
    return None


def parse_dates(date_str):
    match = re.match(r"^(\d{10})-(\d{10})$", date_str)
    if not match:
        raise ValueError(f"Invalid date format '{date_str}'; expected YYYYMMDDHH-YYYYMMDDHH")
    start = datetime.strptime(match.group(1), "%Y%m%d%H").replace(tzinfo=timezone.utc)
    end = datetime.strptime(match.group(2), "%Y%m%d%H").replace(tzinfo=timezone.utc)
    if start >= end:
        raise ValueError("Start date must precede end date")
    return start, end


def usage(error_msg=None):
    if error_msg:
        print(f"Error: {error_msg}\n", file=sys.stderr)
    print(
        f"Usage: {Path(__file__).name} [YYYYMMDDHH-YYYYMMDDHH] [additional automats options...]\n"
        "\n"
        "Makes verification plots comparing real-time 'rrfsv2x' and 'rrfsv2b' against HRRR_OPS.\n"
        "If date range is omitted, defaults to MATS dynamic latest 30-day window from the database.\n"
        "Reuses recipes/baseline.yaml by overwriting its experiments section,\n"
        "and saves a temporary recipe under ./tmp/.\n"
        "\n"
        "Common options:\n"
        "  --output-dir DIR   Output directory (default: results/rt_rrfsv2_<tag>)\n"
        "  --plan             List planned figures without running browser\n"
        "  --metadata-only    Refresh metadata on cached figures locally\n"
        "  --slides-only, --slide-only  Build PowerPoint deck from existing PNGs\n"
        "  --update           Regenerate figures even if they already exist in output directory\n"
        "  --limit N          Generate at most N figures\n"
        "  --show-browser     Run Chrome visibly instead of headless background\n"
        "  -h, --help         Show this help message and exit",
        file=sys.stderr if error_msg else sys.stdout,
    )


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    date_str = None
    forward_args = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            usage()
            return 0
        elif re.match(r"^\d{10}-\d{10}$", arg) and date_str is None:
            date_str = arg
        else:
            forward_args.append(arg)
        i += 1

    start_iso = None
    end_iso = None

    if date_str:
        try:
            start_dt, end_dt = parse_dates(date_str)
            start_iso = start_dt.strftime("%Y-%m-%dT%H:00:00Z")
            end_iso = end_dt.strftime("%Y-%m-%dT%H:00:00Z")
            tag = f"{start_dt.strftime('%Y%m%d%H')}-{end_dt.strftime('%Y%m%d%H')}"
        except ValueError as e:
            usage(str(e))
            return 1
    else:
        # Discover the actual date range from the MATS database to name the output tag
        resolved = fetch_mats_date_range()
        if resolved is not None:
            tag = f"{resolved[0].strftime('%Y%m%d%H')}-{resolved[1].strftime('%Y%m%d%H')}"
        else:
            start_dt, end_dt = compute_default_dates()
            tag = f"{start_dt.strftime('%Y%m%d%H')}-{end_dt.strftime('%Y%m%d%H')}"

    baseline_recipe = ROOT / "recipes" / "baseline.yaml"
    if not baseline_recipe.is_file():
        print(f"Error: Baseline recipe not found at {baseline_recipe}", file=sys.stderr)
        return 1

    with open(baseline_recipe, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Overwrite experiments for real-time comparison
    data["experiments"] = [
        {"MATS name": "rrfsv2x", "color": "red"},
        {"MATS name": "rrfsv2b", "color": "blue"},
        {"MATS name": "HRRR_OPS", "color": "orange"},
    ]

    # Real-time runs should always disable pygraf slides
    if "pygraf slides" in data and isinstance(data["pygraf slides"], dict):
        data["pygraf slides"]["enable"] = False
    else:
        data["pygraf slides"] = {"enable": False}

    # Set start and end dates only when explicitly provided by the user;
    # otherwise leave unset so MATS automatically uses its dynamic latest 30-day window from the database.
    common = data.setdefault("common", {})
    if start_iso and end_iso:
        common["start"] = start_iso
        common["end"] = end_iso
    else:
        common.pop("start", None)
        common.pop("end", None)

    # Save temporary recipe under ./tmp
    tmp_dir = ROOT / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_recipe = tmp_dir / f"rt_rrfsv2_{tag}.yaml"
    with open(tmp_recipe, "w", encoding="utf-8") as f:
        yaml.dump(data, f, sort_keys=False)

    has_output_dir = any(arg == "--output-dir" or arg.startswith("--output-dir=") for arg in forward_args)
    if not has_output_dir:
        forward_args.extend(["--output-dir", str(ROOT / "results" / f"rt_rrfsv2_{tag}")])

    return automats_main([str(tmp_recipe)] + forward_args)


if __name__ == "__main__":
    sys.exit(main())
