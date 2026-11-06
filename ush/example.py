#!/usr/bin/env python3
"""Run example AutoMATS recipe (recipes/example.yaml) with default output to results/example."""

import os
import sys
from pathlib import Path

# Auto-re-exec with local venv python if invoked by system python or another interpreter
ROOT = Path(__file__).resolve().parent.parent
venv_dir = ROOT / ".venv"
venv_python = venv_dir / "bin" / "python"
if venv_python.is_file() and Path(sys.prefix).resolve() != venv_dir.resolve():
    os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve())] + sys.argv[1:])

sys.path.insert(0, str(ROOT / "src"))

from automats.cli import main as automats_main


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    recipe = ROOT / "recipes" / "example.yaml"
    if not recipe.is_file():
        sys.exit(f"Error: Example recipe not found at {recipe}")

    has_output_dir = any(arg == "--output-dir" or arg.startswith("--output-dir=") for arg in argv)
    extra_args = list(argv)
    if not has_output_dir:
        extra_args.extend(["--output-dir", str(ROOT / "results" / "example")])

    return automats_main([str(recipe)] + extra_args)


if __name__ == "__main__":
    sys.exit(main())
