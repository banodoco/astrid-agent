#!/usr/bin/env bash
# Isolated GitHub-hosted user manager. Only redacted capture leaves the VM.
set -Eeuo pipefail
workspace=${GITHUB_WORKSPACE:?} run_id=${GITHUB_RUN_ID:?} attempt=${GITHUB_RUN_ATTEMPT:?}
[[ $run_id =~ ^[0-9]+$ && $attempt =~ ^[0-9]+$ ]]
name="sp${run_id}x${attempt}"; [[ ${#name} -le 32 ]]
out="$workspace/suite-artifacts/r18-peer-authority"; mkdir -p "$out"
run= uid_owned= dropin= created=0 private_log=
finish() {
  rc=$?; trap - EXIT; set +e
  if [[ -n $run ]] && sudo test -f "$run/result.json"; then
    sudo cp "$run/result.json" "$out/result.json"
    while IFS= read -r -d "" export_file; do
      sudo cp -- "$export_file" "$out/"
    done < <(sudo find "$run" -maxdepth 1 -name "export-*.txt" -type f -print0)
    sudo chown -R "$(id -u):$(id -g)" "$out"
  fi
  if [[ $created = 1 && -n $uid_owned ]]; then
    sudo systemctl stop "user@$uid_owned.service" >/dev/null 2>&1; printf 'manager_stop=%s\n' "$?" >> "$out/cleanup.txt"
    sudo loginctl disable-linger "$name" >/dev/null 2>&1; printf 'disable_linger=%s\n' "$?" >> "$out/cleanup.txt"
  fi
  if [[ -n $dropin ]]; then
    sudo rm -f "$dropin/override.conf"; printf 'remove_override=%s\n' "$?" >> "$out/cleanup.txt"
    sudo rmdir "$dropin"; printf 'remove_override_dir=%s\n' "$?" >> "$out/cleanup.txt"
    sudo systemctl daemon-reload >/dev/null 2>&1; printf 'daemon_reload=%s\n' "$?" >> "$out/cleanup.txt"
  fi
  if [[ $created = 1 ]]; then sudo userdel "$name" >/dev/null 2>&1; printf 'userdel=%s\n' "$?" >> "$out/cleanup.txt"; fi
  if [[ -n $private_log ]]; then rm -f -- "$private_log"; fi
  if [[ -n $run ]]; then sudo rm -rf -- "$run"; printf 'scratch_remove=%s\n' "$?" >> "$out/cleanup.txt"; fi
  printf '%s\n' "$rc" > "$out/inner-exit.txt"
  (cd "$out" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 -r sha256sum > SHA256SUMS)
  exit "$rc"
}
trap finish EXIT
[[ $(id -u) -ne 0 && $(ps -p 1 -o comm= | tr -d ' ') = systemd ]]
! getent passwd "$name" >/dev/null
run=$(mktemp -d "/var/tmp/systemd-proof-${run_id}-${attempt}-XXXXXX")
mkdir -p "$run/home" "$run/tmp" "$run/agent" "$run/runtime"
# Runner workspace parent is not traversable by the disposable user.
cp -a agent/astrid agent/pyproject.toml "$run/agent/"
cp -a agent/tests/installer_peer_authority_probe.py "$run/probe.py"
cp -a runtime/runtime_protocol "$run/runtime/"
for repo in agent runtime app pinokio; do printf '%s ' "$repo"; git -C "$repo" rev-parse HEAD; done > "$out/tuple.txt"
sha256sum agent/astrid/installer/lifecycle_transfer_adapter.py agent/tests/installer_peer_authority_{venue.sh,probe.py} > "$out/input-hashes.txt"
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
user_env=(HOME="$run/home" USER="$name" LOGNAME="$name" XDG_RUNTIME_DIR="/run/user/$uid_owned" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid_owned/bus" XDG_CONFIG_HOME="$run/home/.config" TMPDIR="$run/tmp" PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$run/agent:$run/runtime" PROOF_ROOT="$run")
sudo systemctl show "user@$uid_owned.service" -p MainPID -p ActiveState -p Id > "$out/manager-identity.txt"
sudo -u "$name" env "${user_env[@]}" systemctl --user show -p Version > "$out/manager-version.txt"
systemctl --version | head -1 > "$out/client-version.txt"
dpkg-query -W -f='${Package} ${Version}\n' systemd dbus-user-session > "$out/packages.txt"
private_log=$(mktemp "${RUNNER_TEMP:?}/systemd-proof-driver.XXXXXX")
set +e
sudo -u "$name" env "${user_env[@]}" python3 "$run/probe.py" >"$private_log" 2>&1
rc=$?
set -e
python3 - "$private_log" "$out/driver.json" <<'PYCODE'
import hashlib, json, pathlib, re, sys
raw = pathlib.Path(sys.argv[1]).read_bytes()
# Only an exception class, length and digest cross the artifact boundary.
last = raw.splitlines()[-1].decode("utf-8", "replace") if raw.splitlines() else ""
match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))(?::.*)?", last)
pathlib.Path(sys.argv[2]).write_text(json.dumps({"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "error_type": match.group(1) if match else None}) + "\n")
PYCODE
printf 'driver=%s\n' "$rc" > "$out/case-exits.txt"
exit "$rc"
