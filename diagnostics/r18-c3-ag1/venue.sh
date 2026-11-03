#!/usr/bin/env bash
# Disposable systemd venue. Setup is privileged; both collectors run non-root.
set -Eeuo pipefail
workspace=${GITHUB_WORKSPACE:?} run_id=${GITHUB_RUN_ID:?} attempt=${GITHUB_RUN_ATTEMPT:?}
[[ $run_id =~ ^[0-9]+$ && $attempt =~ ^[0-9]+$ ]]
name="sc${run_id}x${attempt}"; [[ ${#name} -le 32 ]]
out="$workspace/suite-artifacts/r18-c3-ag1"; mkdir -p "$out"
run= uid_owned= dropin= created=0
finish() {
  rc=$?; trap - EXIT; set +e
  if [[ -n $run ]]; then
    for file in user-result.json nondelegated-result.json; do
      if sudo test -f "$run/$file"; then sudo cp "$run/$file" "$out/$file"; fi
    done
    sudo chown -R "$(id -u):$(id -g)" "$out"
  fi
  if [[ $created = 1 && -n $uid_owned ]]; then
    sudo systemctl stop "user@$uid_owned.service" >/dev/null 2>&1; printf 'manager_stop=%s\n' "$?" >> "$out/cleanup.txt"
    sudo loginctl disable-linger "$name" >/dev/null 2>&1; printf 'disable_linger=%s\n' "$?" >> "$out/cleanup.txt"
    sudo systemctl stop "user-runtime-dir@$uid_owned.service" >/dev/null 2>&1; printf 'runtime_dir_stop=%s\n' "$?" >> "$out/cleanup.txt"
  fi
  if [[ -n $dropin ]]; then
    sudo rm -f "$dropin/override.conf"; printf 'remove_override=%s\n' "$?" >> "$out/cleanup.txt"
    sudo rmdir "$dropin"; printf 'remove_override_dir=%s\n' "$?" >> "$out/cleanup.txt"
    sudo systemctl daemon-reload >/dev/null 2>&1; printf 'daemon_reload=%s\n' "$?" >> "$out/cleanup.txt"
  fi
  if [[ $created = 1 ]]; then sudo userdel "$name" >/dev/null 2>&1; printf 'userdel=%s\n' "$?" >> "$out/cleanup.txt"; fi
  if [[ -n $run ]]; then sudo rm -rf -- "$run"; printf 'scratch_remove=%s\n' "$?" >> "$out/cleanup.txt"; fi
  printf '%s\n' "$rc" > "$out/inner-exit.txt"
  (cd "$out" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 -r sha256sum > SHA256SUMS)
  exit "$rc"
}
trap finish EXIT
uname -a > "$out/kernel.txt"
for repo in agent runtime app pinokio; do printf '%s ' "$repo"; git -C "$repo" rev-parse HEAD; done > "$out/tuple.txt"
sha256sum agent/diagnostics/r18-c3-ag1/{venue.sh,probe.py,auth_helper.py} > "$out/input-hashes.txt"
[[ $(id -u) -ne 0 && $(ps -p 1 -o comm= | tr -d ' ') = systemd ]]
! getent passwd "$name" >/dev/null
run=$(mktemp -d "/var/tmp/c3-ag1-${run_id}-${attempt}-XXXXXX")
mkdir -p "$run/home" "$run/tmp"
cp agent/diagnostics/r18-c3-ag1/{probe.py,auth_helper.py} "$run/"
sudo useradd --no-create-home --home-dir "$run/home" --shell /bin/bash "$name"; created=1
uid_owned=$(id -u "$name"); [[ $uid_owned -gt 0 ]]
sudo chown -R "$name:$name" "$run"
dropin_candidate="/etc/systemd/system/user@${uid_owned}.service.d"; [[ ! -e $dropin_candidate ]]
sudo mkdir "$dropin_candidate"; dropin=$dropin_candidate
printf '[Service]\nEnvironment="SYSTEMD_UNIT_PATH=%s:"\n' "$run/home/.config/systemd/user" | sudo tee "$dropin/override.conf" >/dev/null
sudo systemctl daemon-reload
sudo loginctl enable-linger "$name"
sudo systemctl start "user-runtime-dir@$uid_owned.service"
sudo systemctl start "user@$uid_owned.service"
for _ in {1..15}; do sudo -u "$name" test -S "/run/user/$uid_owned/bus" && break; sleep 1; done
sudo -u "$name" test -S "/run/user/$uid_owned/bus" || exit 91
user_env=(HOME="$run/home" USER="$name" LOGNAME="$name" XDG_RUNTIME_DIR="/run/user/$uid_owned" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid_owned/bus" XDG_CONFIG_HOME="$run/home/.config" TMPDIR="$run/tmp" PYTHONDONTWRITEBYTECODE=1 PROOF_ROOT="$run")
sudo systemctl show "user@$uid_owned.service" -p MainPID -p ActiveState -p Id -p ControlGroup -p Delegate > "$out/manager-identity.txt"
systemctl --version | head -1 > "$out/systemd-version.txt"
dpkg-query -W -f='${Package} ${Version}\n' systemd dbus-user-session > "$out/packages.txt"
printf 'uid=%s\n' "$uid_owned" > "$out/uid.txt"
printf '%s\n' 'sudo -u USER env USER_ENV systemd-run --user --scope --quiet --unit=c3-collector-*.scope python3 PROOF_ROOT/probe.py user' 'sudo systemd-run --wait --pipe --collect --uid=USER --property=Delegate=no [environment] python3 PROOF_ROOT/probe.py nondelegated' > "$out/commands.txt"
set +e
sudo -u "$name" env "${user_env[@]}" systemd-run --user --scope --quiet --unit="c3-collector-${run_id}.scope" python3 "$run/probe.py" user > "$out/user-driver.log" 2>&1
user_rc=$?
system_unit="c3-nondelegated-${run_id}-${attempt}"
sudo systemd-run --wait --pipe --collect --unit="$system_unit" --uid="$name" --property=Delegate=no \
  --setenv="HOME=$run/home" --setenv="USER=$name" --setenv="LOGNAME=$name" \
  --setenv="XDG_RUNTIME_DIR=/run/user/$uid_owned" --setenv="PROOF_ROOT=$run" \
  --setenv="NONDELEGATED_UNIT=$system_unit.service" \
  --setenv="TMPDIR=$run/tmp" --setenv=PYTHONDONTWRITEBYTECODE=1 \
  python3 "$run/probe.py" nondelegated > "$out/nondelegated-driver.log" 2>&1
nondelegated_rc=$?
set -e
printf 'user_collector=%s\nnondelegated_collector=%s\nadmission=1\n' "$user_rc" "$nondelegated_rc" > "$out/case-exits.txt"
if (( user_rc != 0 || nondelegated_rc != 0 )); then exit 1; fi
