#!/usr/bin/env bash
# GitHub-hosted diagnostic. Creates one new disposable Unix user and its manager.
set -Eeuo pipefail
workspace=${GITHUB_WORKSPACE:?}
runner_temp=${RUNNER_TEMP:?}
run_id=${GITHUB_RUN_ID:?}
attempt=${GITHUB_RUN_ATTEMPT:?}
[[ "$run_id" =~ ^[0-9]+$ && "$attempt" =~ ^[0-9]+$ ]]
name="al${run_id}x${attempt}"
[[ ${#name} -le 32 ]]
out="$workspace/suite-artifacts"
mkdir -p "$out"
run=""
created=0
uid_owned=""
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$out/venue.log"; }
finish() {
  local rc=$?
  trap - EXIT
  set +e
  log "cleanup begin inner_exit=$rc user_created=$created uid=$uid_owned"
  if [[ "$created" = 1 && -n "$uid_owned" ]]; then
    # Stop only the manager of the user created in this invocation.
    sudo systemctl stop "user@$uid_owned.service" >"$out/cleanup-manager.log" 2>&1
    printf '%s\n' "$?" >"$out/cleanup-manager.exit"
    sudo loginctl disable-linger "$name" >"$out/cleanup-linger.log" 2>&1
    printf '%s\n' "$?" >"$out/cleanup-linger.exit"
  fi
  if [[ -n "$run" && -d "$run" ]]; then
    sudo cp -a "$run/evidence" "$out/evidence" 2>"$out/evidence-copy.err"
    printf '%s\n' "$?" >"$out/evidence-copy.exit"
    sudo cp -a "$run/home/.local/state/astrid-runtime/logs" "$out/installer-logs" 2>"$out/installer-copy.err"
    printf '%s\n' "$?" >"$out/installer-copy.exit"
    sudo chown -R "$(id -u):$(id -g)" "$out"
  fi
  if [[ "$created" = 1 ]]; then
    sudo userdel "$name" >"$out/cleanup-user.log" 2>&1
    printf '%s\n' "$?" >"$out/cleanup-user.exit"
  fi
  printf '%s\n' "$rc" > "$out/inner-exit.txt"
  (cd "$out" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 -r sha256sum > SHA256SUMS)
  exit "$rc"
}
trap finish EXIT
log "preflight host=$(uname -a) caller=$(id) pid1=$(ps -p 1 -o comm=)"
[[ $(id -u) -ne 0 ]]
[[ $(ps -p 1 -o comm= | tr -d ' ') = systemd ]]
! getent passwd "$name" >/dev/null
[[ -d "$runner_temp" && ! -L "$runner_temp" ]]
# Do not interfere with another instance bound to the standard health port.
python3 - <<'PY'
import socket
s=socket.socket()
s.bind(('127.0.0.1', 17340))
s.close()
PY
# GitHub's /home/runner is mode 0750. A different disposable user cannot
# traverse RUNNER_TEMP, so create a unique task-owned tree on the ephemeral VM.
run=$(mktemp -d "/var/tmp/lifecycle-${run_id}-${attempt}-XXXXXX")
mkdir -p "$run/home" "$run/agent/scripts" "$run/agent/tests/fixtures" "$run/app/public" "$run/evidence" "$run/tmp"
cp agent/scripts/*.py "$run/agent/scripts/"
cp -a agent/tests/fixtures/release0 "$run/agent/tests/fixtures/"
cp app/public/install app/public/install-manifest.json "$run/app/public/"
cp agent/diagnostics/lifecycle-native-loop/observe.py "$run/observe.py"
# This scratch-only manifest is derived from the named immutable App source.
# It is intentionally diagnostic, not a frozen release manifest or contract.
python3 - "$run/app/public/install-manifest.json" "$run/target-manifest.json" <<'PY'
import json,sys
from pathlib import Path
src,dst=map(Path,sys.argv[1:])
x=json.loads(src.read_text())
assert x.get('release') == 1
x['agent']['sha']='b54117c9312165e2799554ec27480665d5cad2b1'
x['runtime']['sha']='2776ba973b6c8f3555b5629b0840d9691a98d3c2'
x['agent'].pop('repo',None); x['runtime'].pop('repo',None)
dst.write_text(json.dumps(x,indent=2)+'\n')
PY
sha256sum "$run/app/public/install" "$run/app/public/install-manifest.json" "$run/target-manifest.json" "$run/agent/tests/fixtures/release0/SHA256SUMS" > "$out/input-hashes.txt"
for repo in agent runtime app pinokio; do printf '%s ' "$repo"; git -C "$repo" rev-parse HEAD; done >"$out/tuple.txt"
sudo useradd --no-create-home --home-dir "$run/home" --shell /bin/bash "$name"
created=1
uid_owned=$(id -u "$name")
[[ "$uid_owned" -gt 0 ]]
sudo chown -R "$name:$name" "$run"
log "created disposable user=$name uid=$uid_owned run=$run"
sudo loginctl enable-linger "$name"
sudo systemctl start "user-runtime-dir@$uid_owned.service"
sudo systemctl start "user@$uid_owned.service"
for _ in {1..15}; do sudo -u "$name" test -S "/run/user/$uid_owned/bus" && break; sleep 1; done
if ! sudo -u "$name" test -S "/run/user/$uid_owned/bus"; then
  log 'user bus absent; capturing manager and runtime-directory diagnostics'
  sudo systemctl --no-pager --full status "user-runtime-dir@$uid_owned.service" "user@$uid_owned.service" >"$out/manager-status.log" 2>&1 || true
  sudo journalctl --no-pager -b -u "user@$uid_owned.service" -u "user-runtime-dir@$uid_owned.service" >"$out/manager-journal.log" 2>&1 || true
  sudo loginctl show-user "$name" >"$out/loginctl.txt" 2>&1 || true
  sudo ls -la "/run/user/$uid_owned" >"$out/runtime-dir.txt" 2>&1 || true
  dpkg-query -W dbus-user-session dbus-daemon dbus  >"$out/dbus-packages.txt" 2>&1 || true
  namei -l "$run/home" >"$out/home-ancestors.txt" 2>&1 || true
  exit 91
fi
user_env=(HOME="$run/home" USER="$name" LOGNAME="$name" XDG_RUNTIME_DIR="/run/user/$uid_owned" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid_owned/bus" XDG_CONFIG_HOME="$run/home/.config" XDG_DATA_HOME="$run/home/.local/share" XDG_STATE_HOME="$run/home/.local/state" TMPDIR="$run/tmp" ASTRID_RUNTIME_DATA_ROOT="$run/data" ASTRID_CI_PROGRAM_ROOT="$run/program" ASTRID_CI_DATA_ROOT="$run/data" LOOP_ROOT="$run" BROWSER=/bin/true)
sudo -u "$name" env "${user_env[@]}" systemctl --user show -p UnitPath -p Version >"$out/manager-preflight.txt"
unit_path=$(sudo -u "$name" env "${user_env[@]}" systemctl --user show astrid.service -p LoadState --value)
[[ "$unit_path" = not-found ]]
! sudo -u "$name" test -e "$run/home/.config/systemd/user/astrid.service"
log 'starting authentic R0 installer and bounded R1 update observation'
set +e
sudo -u "$name" env "${user_env[@]}" sh -c 'cd "$LOOP_ROOT" && exec python3 "$LOOP_ROOT/observe.py"' >"$out/driver.log" 2>&1
rc=$?
set -e
log "driver exit=$rc"
exit "$rc"
