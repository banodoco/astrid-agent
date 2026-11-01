#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
if [[ "${1:-}" == "--rehearse" ]]; then
  bash -n "$HERE/run.sh"
  python3 "$HERE/runner.py" --syntax-check
  exec python3 "$HERE/runner.py" --rehearse
fi
if [[ "${1:-}" == "--help" ]]; then
  exec python3 "$HERE/runner.py" --help
fi
if [[ "${1:-}" != "--execute" ]]; then
  echo "usage: $0 --rehearse | --execute" >&2
  exit 2
fi
if [[ "${M02_POD_EXECUTION:-}" != "1" ]]; then
  echo "refusing execution outside the pod; use --rehearse locally" >&2
  exit 2
fi
exec python3 "$HERE/runner.py" --execute
