#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
venv_python="$script_dir/.venv/bin/python"

if [ "$#" -gt 1 ]; then
    printf 'Usage: ./setup.sh [python-executable]\n' >&2
    exit 1
fi

python=${1:-python3}
if [ ! -e "$script_dir/.venv" ]; then
    if ! command -v "$python" >/dev/null 2>&1; then
        printf 'Python not found: %s. Install Python 3.10 or newer.\n' "$python" >&2
        exit 1
    fi
    "$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "AutoMATS requires Python 3.10 or newer.")'
    "$python" -m venv "$script_dir/.venv"
elif [ ! -x "$venv_python" ]; then
    printf 'Existing environment is not usable: %s. It has been left unchanged.\n' "$script_dir/.venv" >&2
    exit 1
fi

"$venv_python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "AutoMATS requires Python 3.10 or newer.")'
"$venv_python" -m pip install "$script_dir"
printf 'Setup complete. Run: "%s/run.sh" --help\n' "$script_dir"