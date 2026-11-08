#!/usr/bin/env python3
"""Run baseline comparison: compare <expname> against Gaea C6 baseline."""

import os
import sys
from pathlib import Path
import yaml

# Auto-re-exec with local venv python if invoked by system python or another interpreter
ROOT = Path(__file__).resolve().parent.parent
venv_dir = ROOT / ".venv"
venv_python = venv_dir / "bin" / "python"
if venv_python.is_file() and Path(sys.prefix).resolve() != venv_dir.resolve():
    os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve())] + sys.argv[1:])

sys.path.insert(0, str(ROOT / "src"))

from automats.cli import main as automats_main

BASELINE_EXPERIMENT = "RRFSv2_conus_3km_ret_v2x_retv4"


def usage(error_msg=None):
    if error_msg:
        print(f"Error: {error_msg}\n", file=sys.stderr)
    print(
        f"Usage: {Path(__file__).name} <expname> [additional automats options...]\n"
        "\n"
        f"Compares <expname> against the Gaea C6 baseline ({BASELINE_EXPERIMENT}).\n"
        "Reuses recipes/baseline.yaml by overwriting its experiments section and saving a temporary\n"
        "recipe under ./tmp/.\n"
        "\n"
        "Common options:\n"
        "  --output-dir DIR   Output directory (default: results/baseline_c6_<expname>)\n"
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

    if not argv or argv[0] in ("-h", "--help"):
        usage()
        return 0

    expname = None
    comroot = None
    forward_args = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            usage()
            return 0
        elif arg == "--expname":
            i += 1
            if i < len(argv):
                expname = argv[i]
            else:
                usage("Missing value for --expname")
                return 1
        elif arg.startswith("--expname="):
            expname = arg.split("=", 1)[1]
        elif arg == "--comroot":
            i += 1
            if i < len(argv):
                comroot = argv[i]
            else:
                usage("Missing value for --comroot")
                return 1
        elif arg.startswith("--comroot="):
            comroot = arg.split("=", 1)[1]
        elif not arg.startswith("-") and expname is None:
            expname = arg
        else:
            forward_args.append(arg)
        i += 1

    if not expname:
        usage("Missing required <expname> argument")
        return 1

    baseline_recipe = ROOT / "recipes" / "baseline.yaml"
    if not baseline_recipe.is_file():
        print(f"Error: Baseline recipe not found at {baseline_recipe}", file=sys.stderr)
        return 1

    with open(baseline_recipe, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Find existing definitions in baseline.yaml if present to preserve COMROOT, etc.
    existing_by_name = {
        exp["name"]: exp for exp in data.get("experiments", []) if isinstance(exp, dict) and "name" in exp
    }

    base_exp = dict(existing_by_name.get(BASELINE_EXPERIMENT, {"name": BASELINE_EXPERIMENT}))
    base_exp["color"] = "red"

    target_exp = dict(existing_by_name.get(expname, {"name": expname}))
    target_exp["color"] = "blue"
    if comroot:
        target_exp["COMROOT"] = comroot

    # Overwrite experiments for C6 baseline comparison (baseline first, then target experiment)
    data["experiments"] = [base_exp, target_exp]

    # Save temporary recipe under ./tmp
    tmp_dir = ROOT / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_recipe = tmp_dir / f"baseline_c6_{expname}.yaml"
    with open(tmp_recipe, "w", encoding="utf-8") as f:
        yaml.dump(data, f, sort_keys=False)

    has_output_dir = any(arg == "--output-dir" or arg.startswith("--output-dir=") for arg in forward_args)
    if not has_output_dir:
        forward_args.extend(["--output-dir", str(ROOT / "results" / f"baseline_c6_{expname}")])

    return automats_main([str(tmp_recipe)] + forward_args)


if __name__ == "__main__":
    sys.exit(main())
