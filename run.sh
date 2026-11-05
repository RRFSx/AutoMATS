#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
venv_python="$script_dir/.venv/bin/python"

if [ ! -x "$venv_python" ]; then
    printf 'AutoMATS environment not found. Run: "%s/setup.sh"\n' "$script_dir" >&2
    exit 1
fi

exec "$venv_python" -m automats "$@"