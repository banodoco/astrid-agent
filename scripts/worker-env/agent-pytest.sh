#!/usr/bin/env bash
# Shared astrid-dev utility for single-repo cloud workers; see env/README.md. Copied from plans/02-consistent-generation/env/, unchanged below this line.
# GEN_DESELECT_FILE (optional): a file of pytest node ids, one per line; each becomes --deselect=<id>.
set -euo pipefail
CO=$(pwd -P); E="$CO.env"
test -x "$E/venv/bin/python" || { echo "ENV: run agent-setup.sh first in $CO" >&2; exit 3; }
DES=()
if [ -n "${GEN_DESELECT_FILE:-}" ]; then while IFS= read -r n; do [ -n "$n" ] && DES+=("--deselect=$n"); done < "$GEN_DESELECT_FILE"; fi
BT="$E/basetemp/$(date +%s%N)"
# env -i wipes the caller env: pass through what verify.sh sets (PYTEST_ADDOPTS="-rA ..." gives per-node PASSED lines;
# VERIFY_SIDE = base|head). TMPDIR stays $E/tmp: <checkout>.env is already per side and per run.
exec env -i PATH="$E/venv/bin:$(cat "$E/uv-dir"):/usr/local/bin:/usr/bin:/bin" HOME="$E/home" TMPDIR="$E/tmp" \
  PYTEST_ADDOPTS="${PYTEST_ADDOPTS:-}" VERIFY_SIDE="${VERIFY_SIDE:-}" \
  XDG_CACHE_HOME="$E/cache" LANG=C.UTF-8 PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 \
  PYTHONPATH="$E/vibecomfy" VIBECOMFY_SOURCE="$E/vibecomfy" ASTRID_RUNTIME_CHECKOUT="$E/runtime" \
  "$E/venv/bin/python" -m pytest -p no:cacheprovider --basetemp="$BT" ${DES[@]+"${DES[@]}"} "$@"
