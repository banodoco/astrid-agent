#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /home/user/work/venv-agent/bin/python -I -B "$script_dir/run-generic-reopen-original-0605.py" "$@"
