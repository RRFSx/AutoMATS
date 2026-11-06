#!/bin/sh
set -eu

if [ "$#" -lt 1 ] || [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    printf 'Usage: %s <recipe.yaml> [additional options...]\n' "$0" >&2
    printf 'Calls run.sh with --slides-only to generate PowerPoint slides from existing figures.\n' >&2
    exit 1
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
recipe="$1"
shift

# If recipe path does not exist as given, look in recipes/ or tmp/
if [ ! -f "$recipe" ]; then
    if [ -f "$script_dir/recipes/$recipe" ]; then
        recipe="$script_dir/recipes/$recipe"
    elif [ -f "$script_dir/recipes/${recipe}.yaml" ]; then
        recipe="$script_dir/recipes/${recipe}.yaml"
    elif [ -f "$script_dir/tmp/$recipe" ]; then
        recipe="$script_dir/tmp/$recipe"
    elif [ -f "$script_dir/tmp/${recipe}.yaml" ]; then
        recipe="$script_dir/tmp/${recipe}.yaml"
    fi
fi

# If --output-dir was not passed, auto-detect matching results directory if present
has_output_dir=0
for arg in "$@"; do
    case "$arg" in
        --output-dir|--output-dir=*)
            has_output_dir=1
            break
            ;;
    esac
done

if [ "$has_output_dir" -eq 0 ]; then
    stem=$(basename "$recipe" .yaml)
    if [ -d "$script_dir/results/$stem" ]; then
        set -- --output-dir "$script_dir/results/$stem" "$@"
    elif [ -d "results/$stem" ]; then
        set -- --output-dir "results/$stem" "$@"
    fi
fi

exec "$script_dir/run.sh" "$recipe" --slides-only "$@"
