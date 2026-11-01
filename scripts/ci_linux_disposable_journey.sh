#!/usr/bin/env bash
# Hosted Linux only. Each invocation owns a new UID, manager and temporary tree.
set -Eeuo pipefail
workspace=$(pwd -P)
: "${RUNNER_TEMP:?}" "${RUNNER_OS:?}" "${ImageOS:?}" "${ImageVersion:?}"
base=$(mktemp -d /tmp/astrid-journey-XXXXXXXXXX)
name="astridci$(basename "$base" | tr -cd 'a-zA-Z0-9' | tail -c 10 | tr '[:upper:]' '[:lower:]')"
run_root="$base/run"
export_dir="$RUNNER_TEMP/astrid-r1-direct-linux/evidence"
created=0
uid_owned=
cleanup() {
  status=$?
  trap - EXIT
  if [[ -d "$run_root/evidence" ]]; then
    mkdir -p "$export_dir"
    sudo cp -R "$run_root/evidence/." "$export_dir/" || status=1
    sudo chown -R "$(id -u):$(id -g)" "$export_dir" || status=1
  fi
  if [[ $created == 1 ]] && [[ $(id -u "$name" 2>/dev/null || :) == "$uid_owned" ]]; then
    sudo loginctl disable-linger "$name" || status=1
    sudo loginctl terminate-user "$name" || status=1
    sudo userdel "$name" || status=1
  fi
  case "$base" in /tmp/astrid-journey-*) sudo rm -rf -- "$base" ;; *) status=1 ;; esac
  exit "$status"
}
trap cleanup EXIT
if getent passwd "$name" >/dev/null; then echo 'Disposable username collision' >&2; exit 1; fi
mkdir -p "$run_root/home"
sudo useradd --no-create-home --home-dir "$run_root/home" --shell /bin/bash "$name"
created=1
uid_owned=$(id -u "$name")
chmod 755 "$base" "$run_root" "$run_root/home"
sudo chown -R "$uid_owned:$(id -g "$name")" "$base"
sudo loginctl enable-linger "$name"
for ((i=0; i<30; i++)); do
  [[ -S "/run/user/$uid_owned/bus" ]] && break
  sleep 1
done
[[ -S "/run/user/$uid_owned/bus" ]] || { echo 'Disposable user bus did not start' >&2; exit 1; }
cp "$RUNNER_TEMP/candidate.json" "$base/candidate.json"
chmod 644 "$base/candidate.json"
# Explicit environment excludes provider tokens and never targets the runner's manager.
sudo -u "$name" env -i PATH="$PATH" HOME="$run_root/home" USER="$name" LOGNAME="$name" \
  XDG_RUNTIME_DIR="/run/user/$uid_owned" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid_owned/bus" \
  RUNNER_OS="$RUNNER_OS" ImageOS="$ImageOS" ImageVersion="$ImageVersion" \
  ASTRID_CI_EXPECTED_RUNNER=ubuntu-24.04 ASTRID_CI_DISPOSABLE_USER=1 \
  GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*' \
  python -u "$workspace/agent/scripts/ci_journey.py" \
    --job-id direct-linux --mode direct --leg release0-to-release1 \
    --candidate "$base/candidate.json" --app-checkout "$workspace/app" \
    --candidate-agent-checkout "$workspace/candidate-agent" --candidate-runtime-checkout "$workspace/runtime" \
    --pinokio-checkout "$workspace/pinokio" --run-root "$run_root" --root "$run_root/program" \
    --data-root "$run_root/data" --shell sh --release0-fixture "$workspace/agent/tests/fixtures/release0" \
    --allow-release0-to-release1
