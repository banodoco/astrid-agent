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

for d in "${SRC_DIRS[@]}"; do
  [ -d "$d" ] || { echo "pod_bootstrap: missing source dir $d" >&2; exit 1; }
done

# Digest of the source trees (content + relative path), so the marker is per source set.
digest="$(
  for d in "${SRC_DIRS[@]}"; do
    find "$d" -type f -not -path '*/.git/*' -not -name '*.pyc' -not -path '*/__pycache__/*' -print0
  done | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16
)"
marker_dir="/workspace/astrid/bootstrap/$digest"
marker="/workspace/astrid/bootstrap/$digest/ok"

if [ -f "$marker" ]; then
  echo "pod_bootstrap: already done ($marker)"
  exit 0
fi

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

mkdir -p "$marker_dir"
date -u +%Y-%m-%dT%H:%M:%SZ > "$marker"
echo "pod_bootstrap: ok ($marker)"
