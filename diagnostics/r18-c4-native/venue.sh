#!/usr/bin/env bash
# EMULATION: root restarts only a newly-created disposable user manager on an ephemeral Actions runner.
set -Eeuo pipefail
workspace=${GITHUB_WORKSPACE:?} run_id=${GITHUB_RUN_ID:?} attempt=${GITHUB_RUN_ATTEMPT:?}
[[ ${GITHUB_ACTIONS:-} == true && $run_id =~ ^[0-9]+$ && $attempt =~ ^[0-9]+$ ]]
[[ $(id -u) -ne 0 && $(ps -p 1 -o comm= | tr -d ' ') = systemd ]]
name="c4${run_id}x${attempt}"; [[ ${#name} -le 32 ]]
out="$workspace/suite-artifacts/r18-c4-native"; mkdir -p "$out"
run= uid_owned= created=0 unit="c4-${run_id}-${attempt}"
finish() {
  rc=$?; trap - EXIT; set +e
  if [[ -n $run && -d $run/results ]]; then sudo cp -a "$run/results/." "$out/"; fi
  if [[ $created = 1 && -n $uid_owned ]]; then
    sudo systemctl stop "$unit.timer" "$unit.service" >>"$out/cleanup.log" 2>&1; printf 'transient_stop=%s\n' "$?" >>"$out/cleanup.log"
    sudo systemctl stop "user@$uid_owned.service" >>"$out/cleanup.log" 2>&1; printf 'manager_stop=%s\n' "$?" >>"$out/cleanup.log"
    sudo loginctl disable-linger "$name" >>"$out/cleanup.log" 2>&1; printf 'disable_linger=%s\n' "$?" >>"$out/cleanup.log"
    sudo systemctl stop "user-runtime-dir@$uid_owned.service" >>"$out/cleanup.log" 2>&1; printf 'runtime_dir_stop=%s\n' "$?" >>"$out/cleanup.log"
    sudo userdel "$name" >>"$out/cleanup.log" 2>&1; printf 'userdel=%s\n' "$?" >>"$out/cleanup.log"
  fi
  if [[ -n $run ]]; then sudo rm -rf -- "$run"; printf 'scratch_remove=%s\n' "$?" >>"$out/cleanup.log"; fi
  sudo chown -R "$(id -u):$(id -g)" "$out"
  printf '%s\n' "$rc" > "$out/venue-exit.txt"
  (cd "$out" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 -r sha256sum > SHA256SUMS)
  exit "$rc"
}
trap finish EXIT
! getent passwd "$name" >/dev/null
run=$(mktemp -d "/var/tmp/c4-${run_id}-${attempt}-XXXXXX")
mkdir -p "$run/home" "$run/tmp" "$run/results" "$run/route-controls"/{clean,cron,at,xdg,system,inaccessible,foreign}
cp agent/diagnostics/r18-c4-native/{collector.py,evaluate.py} "$run/"
sudo useradd --no-create-home --home-dir "$run/home" --shell /bin/bash "$name"; created=1
uid_owned=$(id -u "$name"); [[ $uid_owned -gt 0 ]]
printf 'fixture cron /bin/true\n' > "$run/route-controls/cron/cron-entry"
printf 'fixture at /bin/true\n' > "$run/route-controls/at/at-entry"
printf '[Desktop Entry]\nExec=/bin/true\n' > "$run/route-controls/xdg/c4.desktop"
printf 'fixture systemd-run --system --uid %s\n' "$name" > "$run/route-controls/system/transient-unit"
ln -s /etc/shadow "$run/route-controls/foreign/foreign-link"
sudo chown -R "$name:$name" "$run"
sudo chown root:root "$run/route-controls/inaccessible"
sudo chmod 000 "$run/route-controls/inaccessible"
sudo loginctl enable-linger "$name"
sudo systemctl start "user-runtime-dir@$uid_owned.service"
sudo systemctl start "user@$uid_owned.service"
for _ in {1..15}; do sudo -u "$name" test -S "/run/user/$uid_owned/bus" && break; sleep 1; done
sudo -u "$name" test -S "/run/user/$uid_owned/bus" || exit 91
user_env=(HOME="$run/home" USER="$name" LOGNAME="$name" XDG_RUNTIME_DIR="/run/user/$uid_owned" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid_owned/bus" XDG_CONFIG_HOME="$run/home/.config" TMPDIR="$run/tmp" C4_ROOT="$run" C4_SYSTEM_UNIT="$unit.service" PYTHONDONTWRITEBYTECODE=1)
uname -a > "$out/kernel.txt"
systemctl --version | head -1 > "$out/systemd.txt"
for repo in agent runtime app pinokio; do printf '%s ' "$repo"; git -C "$repo" rev-parse HEAD; done > "$out/tuple.txt"
sha256sum agent/diagnostics/r18-c4-native/{venue.sh,collector.py,evaluate.py} > "$out/input-hashes.txt"
printf 'uid=%s\nuser=%s\nroot=%s\n' "$uid_owned" "$name" "$run" > "$out/account.txt"
printf '%q ' sudo -u "$name" env "${user_env[@]}" python3 "$run/collector.py" before > "$out/before-command.txt"; printf '\n' >> "$out/before-command.txt"
printf '%q ' sudo -u "$name" env "${user_env[@]}" python3 "$run/collector.py" after > "$out/after-command.txt"; printf '\n' >> "$out/after-command.txt"
printf '%q ' sudo -u "$name" env "${user_env[@]}" python3 "$run/evaluate.py" > "$out/evaluate-command.txt"; printf '\n' >> "$out/evaluate-command.txt"
sudo systemctl show "user@$uid_owned.service" -p MainPID -p ActiveState -p Id > "$out/manager-before.txt"
set +e
sudo -u "$name" env "${user_env[@]}" python3 "$run/collector.py" before > "$out/before.log" 2>&1; before_rc=$?
# A pending inert transient timer demonstrates a root-created system spawn route.
sudo systemd-run --unit="$unit" --on-active=1d --uid="$name" /bin/true > "$out/system-route.log" 2>&1; system_route_rc=$?
sudo systemctl restart "user@$uid_owned.service" > "$out/restart.log" 2>&1; restart_rc=$?
for _ in {1..15}; do sudo -u "$name" test -S "/run/user/$uid_owned/bus" && break; sleep 1; done
sudo systemctl show "user@$uid_owned.service" -p MainPID -p ActiveState -p Id > "$out/manager-after.txt"
sudo -u "$name" env "${user_env[@]}" python3 "$run/collector.py" after > "$out/after.log" 2>&1; after_rc=$?
sudo -u "$name" env "${user_env[@]}" python3 "$run/evaluate.py" > "$out/evaluate.log" 2>&1; evaluate_rc=$?
printf 'before_collector=%s\nsystem_route_setup=%s\nmanager_restart=%s\nafter_collector=%s\nevaluator=%s\nadmission=1\n' "$before_rc" "$system_route_rc" "$restart_rc" "$after_rc" "$evaluate_rc" > "$out/case-exits.txt"
set -e
# Diagnostic health is distinct from refusal; all nested exits remain in artifacts.
(( before_rc == 0 && system_route_rc == 0 && restart_rc == 0 && after_rc == 0 && evaluate_rc == 1 ))
