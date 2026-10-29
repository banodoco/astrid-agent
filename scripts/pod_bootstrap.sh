#!/usr/bin/env bash
# On-pod bootstrap (once per pod). Run from the directory that holds the five
# uploaded source dirs: src/runpod-python src/astrid-runtime src/runpod-lifecycle
# src/astrid-agent src/VibeComfy. Idempotent. Takes no auth input.
# Job budget: every job start and execute is one agent job record (cap 128); poll with `job poll` (GET state, no record).
set -euo pipefail

PY_VERSION="3.12.10"
VENV="${ASTRID_VENV:-/opt/astrid}"
RUNPOD_PRETEND_VERSION="1.12.0+astrid.2"
SRC_DIRS=(src/runpod-python src/astrid-runtime src/runpod-lifecycle src/astrid-agent src/VibeComfy)
PACKAGES=(runpod astrid-runtime runpod-lifecycle astrid vibecomfy)
# Headless Runtime setup. The data root follows astrid_runtime/paths.py on Linux.
DATA_ROOT="${ASTRID_RUNTIME_DATA_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/astrid-runtime}"
REALM_ROOT="${ASTRID_REALM_ROOT:-/workspace/astrid/realm}"
BOOTSTRAP_ROOT="${ASTRID_BOOTSTRAP_ROOT:-/workspace/astrid/bootstrap}"
ENGINE_ROOT="${ASTRID_ENGINE_ROOT:-/workspace/astrid/engines/vibecomfy-h3}"
# uv caches and managed Pythons live under the data root unless the caller set them.
export UV_CACHE_DIR="${UV_CACHE_DIR:-$DATA_ROOT/uv-cache}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$DATA_ROOT/uv-python}"

# Create a directory we own, or fail clearly. Never chown, never needs root.
ensure_dir() {
  if ! mkdir -p "$1" 2>/dev/null || [ ! -d "$1" ] || [ ! -w "$1" ]; then
    echo "pod_bootstrap: cannot create or write $1 as $(id -un); point the matching ASTRID_* variable at a path you own (this script does not chown)" >&2
    exit 1
  fi
}

for d in "${SRC_DIRS[@]}"; do
  [ -d "$d" ] || { echo "pod_bootstrap: missing source dir $d" >&2; exit 1; }
done

# Digest of the source trees (content + relative path), so the marker is per source set.
digest="$(
  for d in "${SRC_DIRS[@]}"; do
    find "$d" -type f -not -path '*/.git/*' -not -name '*.pyc' -not -path '*/__pycache__/*' -print0
  done | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16
)"
marker_dir="$BOOTSTRAP_ROOT/$digest"
marker="$marker_dir/ok"
ensure_dir "$BOOTSTRAP_ROOT"
ensure_dir "$DATA_ROOT"
ensure_dir "$(dirname "$REALM_ROOT")"

# Headless Runtime setup: a source profile plus a configured workspace, so
# `astrid action run` works with no manual step. It never runs `astrid-runtime up`:
# a running Runtime holds the door port (17340) and `astrid action run` refuses to
# start its own beside it. `action run` starts and stops its own ephemeral Runtime.
setup_runtime() {
  local profile="$DATA_ROOT/runtime/source-profiles/astrid.json"
  local runtime_src agent_src
  runtime_src="$(cd src/astrid-runtime && pwd)"
  agent_src="$(cd src/astrid-agent && pwd)"
  mkdir -p "$(dirname "$profile")"
  if "$VENV/bin/python" - "$profile" "$runtime_src" "$agent_src" "$VENV" <<'PY'
import json, sys
path, runtime, agent, env = sys.argv[1:]
want = {"profile": "astrid", "runtime_checkout": runtime, "source_checkout": agent, "runtime_environment": env}
try:
    with open(path, encoding="utf-8") as fh:
        sys.exit(0 if json.load(fh) == want else 1)
except (OSError, ValueError):
    sys.exit(1)
PY
  then
    echo "pod_bootstrap: source profile already current ($profile)"
  else
    "$VENV/bin/python" - "$profile" "$runtime_src" "$agent_src" "$VENV" <<'PY'
import json, os, sys
path, runtime, agent, env = sys.argv[1:]
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump({"profile": "astrid", "runtime_checkout": runtime, "source_checkout": agent, "runtime_environment": env}, fh)
os.replace(tmp, path)
PY
    echo "pod_bootstrap: wrote source profile ($profile)"
  fi
  if [ -e "$REALM_ROOT/realm.sqlite3" ]; then
    echo "pod_bootstrap: workspace already exists ($REALM_ROOT)"
  else
    mkdir -p "$(dirname "$REALM_ROOT")"
    "$VENV/bin/astrid-runtime" workspace create --data-root "$DATA_ROOT" --realm-root "$REALM_ROOT" --json
  fi
}

# The engine's FIX-RECEIPT-PROBE change lives in these two astrid modules. astrid is installed
# non-editable, so the installed copy must be the one from src/astrid-agent: a hand-copied file or
# any other drift would run the old probe with no warning. Compare the modules by sha256 and
# reinstall astrid only on a mismatch or a missing copy. Cheap enough to run on every bootstrap.
PROBE_MODULES=(engine_runner.py engine_runner_target.py)
verify_astrid_install() {
  local rel want got site
  site="$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  for rel in "${PROBE_MODULES[@]}"; do
    want="$(sha256sum "src/astrid-agent/astrid/core/generation/$rel" | cut -d' ' -f1)"
    got="$(sha256sum "$site/astrid/core/generation/$rel" 2>/dev/null | cut -d' ' -f1)" || got=""
    if [ "$want" != "$got" ]; then
      echo "pod_bootstrap: installed astrid differs from src/astrid-agent ($rel); reinstalling astrid"
      uv pip install --python "$VENV" --reinstall-package astrid src/astrid-agent
      return 0
    fi
  done
  echo "pod_bootstrap: installed astrid matches src/astrid-agent"
}

# GPU sanity (bf16/fp16/fp32 GEMM) under the engine's own child env. Runs only when the engine venv
# already exists; a fresh claim has none yet, so it says so and skips. Fails closed (exit 2).
run_gpu_sanity() {
  local engine_venv="$ENGINE_ROOT/runtime/venv" py=python3
  if [ ! -x "$engine_venv/bin/python" ]; then
    echo "pod_bootstrap: gpu sanity skipped: no engine venv yet (it runs on a rerun, or inside the engine install)"
    return 0
  fi
  [ -x "$VENV/bin/python" ] && py="$VENV/bin/python"
  "$py" src/astrid-agent/scripts/gpu_sanity.py --venv "$engine_venv" || {
    echo "pod_bootstrap: gpu sanity FAILED for $engine_venv" >&2
    exit 2
  }
}

# Engine install hook: engine_runner.install_engine runs this command (plus --venv <engine venv>) after the
# environment steps and before the model downloads, and aborts the install if it fails. Absolute path, so the
# install may run from any cwd. Export it in the shell that runs `astrid engines install`.
SANITY_PY="$VENV/bin/python"
[ -x "$SANITY_PY" ] || SANITY_PY=python3
export ASTRID_ENGINE_SANITY_CMD="$SANITY_PY $(cd src/astrid-agent && pwd)/scripts/gpu_sanity.py"
echo "pod_bootstrap: for the engine install run: export ASTRID_ENGINE_SANITY_CMD='$ASTRID_ENGINE_SANITY_CMD'"

if [ -f "$marker" ]; then
  echo "pod_bootstrap: already done ($marker)"
  run_gpu_sanity
  verify_astrid_install
  setup_runtime
  exit 0
fi

# Fresh claim: no engine venv yet, so this says so and skips.
run_gpu_sanity

# uv is never fetched here (no piping a download into a shell): use the one on PATH or stop.
if ! command -v uv >/dev/null 2>&1; then
  echo "pod_bootstrap: uv not found on PATH. Install uv (https://docs.astral.sh/uv/getting-started/installation/) and rerun; this script does not download it." >&2
  exit 1
fi
ensure_dir "$UV_CACHE_DIR"
ensure_dir "$UV_PYTHON_INSTALL_DIR"
ensure_dir "$(dirname "$VENV")"

have_all() {
  [ -x "$VENV/bin/python" ] || return 1
  local installed
  installed="$(uv pip list --python "$VENV" --format freeze 2>/dev/null)" || return 1
  local p
  for p in "${PACKAGES[@]}"; do
    printf '%s\n' "$installed" | grep -qiE "^${p//-/[-_]}==" || return 1
  done
}

if have_all; then
  echo "pod_bootstrap: $VENV already has the packages; skipping install"
else
  uv python install "$PY_VERSION"
  [ -x "$VENV/bin/python" ] || uv venv --python "$PY_VERSION" "$VENV"
  SETUPTOOLS_SCM_PRETEND_VERSION_FOR_RUNPOD="$RUNPOD_PRETEND_VERSION" \
    uv pip install --python "$VENV" "${SRC_DIRS[@]}"
fi

# pip in the venv. The engine install's helper runs `python3 -m pip` for its uv bootstrap, and a
# venv made by `uv venv` has no pip. This sits outside the if above, so a venv that already holds
# the packages gets it too. uv leaves an installed pip alone, so this is idempotent.
uv pip install --python "$VENV" pip
verify_astrid_install

setup_runtime

mkdir -p "$marker_dir"
date -u +%Y-%m-%dT%H:%M:%SZ > "$marker"
echo "pod_bootstrap: ok ($marker)"
