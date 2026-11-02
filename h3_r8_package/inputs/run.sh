#!/usr/bin/env bash
set -euo pipefail
base=$(cd -- "$(dirname -- "$0")" && pwd)
if [[ ${1:-} == --rehearse && $# == 4 ]]; then
  exec "$2" -I "$base/driver.py" --rehearse --root "$3" --job "$4"
elif [[ ${1:-} == --canary && $# == 5 ]]; then
  exec "$2" -I "$base/driver.py" --canary --root "$3" --job "$4" --canary-lane "$5"
elif [[ ${1:-} == --pod && $# == 1 ]]; then
  exec python3 -I "$base/driver.py" --pod --root /opt/astrid-h91r2 --job /workspace/jobs/H-91r2-h3-r8-fresh-install
fi
echo 'usage: run.sh --rehearse PYTHON ROOT JOB | --pod' >&2
exit 2
