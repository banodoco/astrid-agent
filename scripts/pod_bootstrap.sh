#!/usr/bin/env bash
# On-pod bootstrap (once per pod). Run from the directory that holds the five
# uploaded source dirs: src/runpod-python src/astrid-runtime src/runpod-lifecycle
# src/astrid-agent src/VibeComfy. Idempotent. Takes no auth input.
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

if [ -f "$marker" ]; then
  echo "pod_bootstrap: already done ($marker)"
  setup_runtime
  exit 0
fi

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

# VibeComfy stays editable: find_repo_root() looks for pyproject.toml beside the imported
# package, which a site-packages copy lacks. --no-deps keeps the dependency set unchanged.
# Runs before the marker is written, so a failed run is retried and a rerun still skips.
uv pip install --python "$VENV" --no-deps -e src/VibeComfy

setup_runtime

mkdir -p "$marker_dir"
date -u +%Y-%m-%dT%H:%M:%SZ > "$marker"
echo "pod_bootstrap: ok ($marker)"
