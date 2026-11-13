#!/usr/bin/env bash
# TEST runner for the RunPod pod-path harness in an astrid-agent checkout (shared astrid-dev utility; see env/README.md).
# Run from the checkout root after agent-setup.sh, which also fetches runpod-lifecycle at its pin into <checkout>.env/lifecycle.
# Args go to pytest verbatim; integration tests are selected.
set -euo pipefail
CO=$(pwd -P); E="$CO.env"
test -x "$E/venv/bin/python" || { echo "ENV: run agent-setup.sh first in $CO" >&2; exit 3; }
test -d "$E/lifecycle" || { echo "ENV: rerun agent-setup.sh (it fetches $E/lifecycle)" >&2; exit 3; }
exec env -i PATH="$E/venv/bin:$(cat "$E/uv-dir"):/usr/local/bin:/usr/bin:/bin" HOME="$E/home" TMPDIR="$E/tmp" \
  XDG_CACHE_HOME="$E/cache" LANG=C.UTF-8 PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 \
  PYTHONPATH="$E/vibecomfy" ASTRID_RUNTIME_CHECKOUT="$E/runtime" \
  ASTRID_VIBECOMFY_CHECKOUT="$E/vibecomfy" ASTRID_TEST_LIFECYCLE_CHECKOUT="$E/lifecycle" \
  "$E/venv/bin/python" -m pytest -q -p no:cacheprovider --basetemp="$E/basetemp/$(date +%s%N)" -m integration "$@"
