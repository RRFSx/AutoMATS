#!/usr/bin/env python3
"""Run baseline comparison: compare <expname> against Gaea C6 baseline."""

import inspect
import os
from pathlib import Path
import re
import sys
import yaml

# Auto-re-exec with local venv python if invoked by system python or another interpreter
ROOT = Path(__file__).resolve().parent.parent
venv_dir = ROOT / ".venv"
venv_python = venv_dir / "bin" / "python"
if __name__ == "__main__" and venv_python.is_file() and Path(sys.prefix).resolve() != venv_dir.resolve():
    os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve())] + sys.argv[1:])

sys.path.insert(0, str(ROOT / "src"))

from automats.cli import main as automats_main

BASELINE_EXPERIMENT = "RRFSv2_conus_3km_ret_v2x_retv5"
BASELINE_SHORT_NAME = "base5"
BASELINE_HPC_DIR = "/gpfs/f6/bil-fire10-oar/world-shared/jjh/rrfsv2/RRFSv2X/L60/OPSROOT/RRFSv2.retv5/com/rrfs/v2.1.4"


def resolve_comroot(hpc_dir: str) -> str:
    """
    Normalizes an HPC directory / COMROOT string into `<cluster>:<path>`.
    - Existing `<cluster>:<path>` prefixes are preserved (lowercased cluster).
    - Bare paths starting with `/gpfs/f6` map to `gaeac6:<path>`.
    - Bare paths starting with `/gpfs/f7` map to `gaeac7:<path>`.
    """
    raw = str(hpc_dir).strip()
    if not raw:
        raise ValueError("HPC directory cannot be empty.")

    m_cluster = re.match(r"^([A-Za-z0-9_-]+):(.*)$", raw)
    if m_cluster:
        cluster = m_cluster.group(1).strip().lower()
        path = m_cluster.group(2).strip()
        if not path:
            raise ValueError(f"Missing path after cluster prefix in '{raw}'.")
        clean_path = path.rstrip("/") or "/"
        return f"{cluster}:{clean_path}"

    clean_path = raw.rstrip("/") or "/"
    if clean_path == "/gpfs/f6" or clean_path.startswith("/gpfs/f6/"):
        return f"gaeac6:{clean_path}"
    if clean_path == "/gpfs/f7" or clean_path.startswith("/gpfs/f7/"):
        return f"gaeac7:{clean_path}"

    raise ValueError(
        f"Cannot infer HPC cluster from '{raw}'. Paths starting with /gpfs/f6 map to gaeac6 "
        "and /gpfs/f7 map to gaeac7; otherwise prefix with <cluster>: (e.g. ursa:/path)."
    )


def usage(error_msg=None):
    if error_msg:
        print(f"Error: {error_msg}\n", file=sys.stderr)
    print(
        f"Usage: {Path(__file__).name} <expname> <short_name> [hpc_dir] [additional automats options...]\n"
        "\n"
        f"Compares <expname> (experiment name in MATS) against the Gaea C6 baseline ({BASELINE_EXPERIMENT}).\n"
        f"Uses <short_name> for figure legends and titles (baseline uses '{BASELINE_SHORT_NAME}'), while the\n"
        "first overview slide displays both the short name and full MATS experiment name.\n"
        "If [hpc_dir] is provided, sets COMROOT for <expname> to download pygraf graphics:\n"
        "  - /gpfs/f6/... -> gaeac6:/gpfs/f6/...\n"
        "  - /gpfs/f7/... -> gaeac7:/gpfs/f7/...\n"
        "  - <cluster>:/path -> preserved as-is (e.g. ursa:/scratch3/...)\n"
        "If [hpc_dir] is omitted, no graphics are downloaded for <expname> (baseline and obs graphics still download).\n"
        "Reuses recipes/baseline.yaml by overwriting its experiments section and saving a temporary\n"
        "recipe under ./tmp/.\n"
        "\n"
        "Common options:\n"
        "  --expname NAME     Experiment name in MATS (alternative to positional <expname>)\n"
        "  --short-name NAME  Short display label for <expname> (alternative to positional <short_name>)\n"
        "  --hpc-dir, --comroot DIR\n"
        "                     HPC directory (COMROOT) for downloading graphics\n"
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
    short_name = None
    hpc_dir = None
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
        elif arg in ("--short-name", "--short_name"):
            i += 1
            if i < len(argv):
                short_name = argv[i]
            else:
                usage(f"Missing value for {arg}")
                return 1
        elif arg.startswith(("--short-name=", "--short_name=")):
            short_name = arg.split("=", 1)[1]
        elif arg in ("--comroot", "--hpc-dir", "--hpc_dir", "--dir"):
            i += 1
            if i < len(argv):
                hpc_dir = argv[i]
            else:
                usage(f"Missing value for {arg}")
                return 1
        elif arg.startswith(("--comroot=", "--hpc-dir=", "--hpc_dir=", "--dir=")):
            hpc_dir = arg.split("=", 1)[1]
        elif arg in ("--output-dir", "--limit"):
            forward_args.append(arg)
            i += 1
            if i < len(argv):
                forward_args.append(argv[i])
            else:
                usage(f"Missing value for {arg}")
                return 1
        elif arg.startswith("-"):
            forward_args.append(arg)
        elif expname is None:
            expname = arg
        elif short_name is None:
            short_name = arg
        elif hpc_dir is None:
            hpc_dir = arg
        else:
            usage(f"Unexpected positional argument: {arg}")
            return 1
        i += 1

    if not expname or not expname.strip():
        usage("Missing required <expname> argument")
        return 1
    expname = expname.strip()

    if "/" in expname or ":" in expname or not re.search(r"[A-Za-z0-9]", expname):
        usage(f"Invalid <expname> '{expname}': expected <expname> <short_name> [hpc_dir]")
        return 1

    if short_name is None or not short_name.strip():
        usage("Missing required <short_name> argument")
        return 1
    short_name = short_name.strip()

    if "/" in short_name or ":" in short_name or not re.search(r"[A-Za-z0-9]", short_name):
        usage(
            f"Invalid <short_name> '{short_name}': expected <expname> <short_name> [hpc_dir] "
            "(short_name cannot be an HPC path containing '/' or ':')"
        )
        return 1

    resolved_comroot = None
    if hpc_dir is not None:
        try:
            resolved_comroot = resolve_comroot(hpc_dir)
        except ValueError as e:
            usage(str(e))
            return 1

    baseline_recipe = ROOT / "recipes" / "baseline.yaml"
    if not baseline_recipe.is_file():
        print(f"Error: Baseline recipe not found at {baseline_recipe}", file=sys.stderr)
        return 1

    with open(baseline_recipe, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    base_exp = {
        "MATS name": BASELINE_EXPERIMENT,
        "short name": BASELINE_SHORT_NAME,
        "color": "red",
        "COMROOT": resolve_comroot(BASELINE_HPC_DIR),
    }

    target_exp = {
        "MATS name": expname,
        "short name": short_name,
        "color": "blue",
    }
    if resolved_comroot:
        target_exp["COMROOT"] = resolved_comroot

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

    pre_slides_warnings = []
    if not resolved_comroot:
        pre_slides_warnings.append(
            f"Warning: no pygraf graphics downloaded for '{expname}' as hpc_dir is missing."
        )

    cli_args = [str(tmp_recipe)] + forward_args
    if pre_slides_warnings and "pre_slides_warnings" in inspect.signature(automats_main).parameters:
        rc = automats_main(cli_args, pre_slides_warnings=pre_slides_warnings)
    else:
        rc = automats_main(cli_args)
        if rc == 0 and pre_slides_warnings:
            sys.stdout.flush()
            for warning in pre_slides_warnings:
                print(warning, file=sys.stderr, flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
