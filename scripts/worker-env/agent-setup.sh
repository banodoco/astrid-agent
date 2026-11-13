#!/usr/bin/env bash
# Worker-safe setup for an astrid-agent checkout (copied from astrid-dev env/; see scripts/worker-env/README.md).
# Worker-safe: other repositories are fetched only with git at their pinned commit; nothing downloads a GitHub archive.
# Run from the root of an astrid-agent checkout. Builds <checkout>.env/ next to the checkout (never inside it):
#   venv (Python 3.13, `-e .[dev]`, the Runtime + client pinned by runtime.lock.json, as CI's scripts/ci_install_runtime.py),
#   runtime/   = banodoco/astrid-runtime at runtime.lock.json source_commit (ASTRID_RUNTIME_CHECKOUT),
#   vibecomfy/ = VibeComfy at the checkout's own VIBECOMFY_ENGINE_REVISION (on PYTHONPATH and in ASTRID_VIBECOMFY_CHECKOUT).
# Needs: uv, git, ffmpeg, network reads to PyPI and GitHub. Any failure here is an ENV error, never a test result.
set -euo pipefail
for t in uv git ffmpeg; do command -v "$t" >/dev/null || { echo "ENV: missing tool $t" >&2; exit 3; }; done
CO=$(pwd -P); E="$CO.env"
test -f "$CO/astrid/__init__.py" -a -f "$CO/runtime.lock.json" || { echo "ENV: not an astrid-agent checkout: $CO" >&2; exit 3; }
rm -rf "$E"; mkdir -p "$E/home" "$E/tmp" "$E/cache" "$E/basetemp"
uv venv -q --seed --python 3.13 "$E/venv"
PY="$E/venv/bin/python"
"$PY" -m pip install -q --disable-pip-version-check --no-input -e "$CO[dev]"
fetch_at() { # url dir sha: exact commit, detached, verified
  git init -q "$2"; git -C "$2" fetch -q --depth=1 "$1" "$3"; git -C "$2" checkout -q --detach FETCH_HEAD
  test "$(git -C "$2" rev-parse HEAD)" = "$3" || { echo "ENV: $1 did not give $3" >&2; exit 3; }; }
RT=$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1]))["source_commit"])' "$CO/runtime.lock.json")
fetch_at https://github.com/banodoco/astrid-runtime "$E/runtime" "$RT"
# Runtime and its Python client, --no-deps, from the verified checkout (as scripts/ci_install_runtime.py, without the archive URL).
"$PY" -m pip install -q --disable-pip-version-check --no-input --no-deps "$E/runtime" "$E/runtime/packages/python"
LC=$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1]))["sources"]["lifecycle"]["revision"])' "$CO/astrid/packs/runpod/pins.json")
fetch_at https://github.com/banodoco/runpod-lifecycle "$E/lifecycle" "$LC"
VC=$(cd "$E" && "$PY" -c 'from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION as r; print(r)')
fetch_at https://github.com/banodoco/VibeComfy "$E/vibecomfy" "$VC"
dirname "$(command -v uv)" > "$E/uv-dir"
printf 'python=%s\nruntime=%s\nvibecomfy=%s\nlifecycle=%s\n' "$("$PY" -c 'import sys; print(sys.version.split()[0])')" "$RT" "$VC" "$LC" | tee "$E/setup.txt"
