#!/usr/bin/env bash
# Compact copy of the journey job's "Establish Linux user service manager" step (hosted ubuntu-24.04 only), for the
# update/rollback job: an isolated systemd user manager whose unit search path is the run root's home.
# usage: ci_linux_user_manager.sh RUN_ROOT   (RUN_ROOT must not exist yet)
set -Eeuo pipefail
run_root="$1"
uid=$(id -u)
export XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus"
config="$run_root/home/.config"; data="$run_root/home/.local/share"; probe_dir="$config/systemd/user"
[[ "$run_root" =~ ^/[a-zA-Z0-9_./-]+$ ]]
test ! -e "$run_root"
timeout 20s sudo loginctl enable-linger "$uid"
timeout 30s sudo systemctl start "user@$uid.service"
for _ in {1..20}; do
  if [[ -S "$XDG_RUNTIME_DIR/bus" ]] && timeout 3s systemctl --user show-environment >/dev/null; then break; fi
  sleep 1
done
test "$(stat -c %u "$XDG_RUNTIME_DIR")" = "$uid"
test -S "$XDG_RUNTIME_DIR/bus"
test ! -e "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/astrid.service"
test "$(timeout 10s systemctl --user show astrid.service --property=LoadState --value || true)" = not-found
dropin="/run/systemd/system/user@$uid.service.d"
sudo mkdir -p "$dropin"
printf '[Service]\nEnvironment="XDG_CONFIG_HOME=%s" "XDG_DATA_HOME=%s" "SYSTEMD_UNIT_PATH=%s:"\n' "$config" "$data" "$probe_dir" | sudo tee "$dropin/90-astrid-journey.conf" >/dev/null
sudo systemctl daemon-reload
timeout 30s sudo systemctl restart "user@$uid.service"
for _ in {1..20}; do timeout 3s systemctl --user show-environment >/dev/null && break; sleep 1; done
timeout 10s systemctl --user set-environment "XDG_CONFIG_HOME=$config" "XDG_DATA_HOME=$data"
activation=$(timeout 10s systemctl --user show-environment)
for expected in "XDG_CONFIG_HOME=$config" "XDG_DATA_HOME=$data"; do
  grep -Fxq "$expected" <<<"$activation" || { echo "::error::user manager lacks $expected"; exit 1; }
done
# The manager must discover units in the exact future install directory (same probe as the journey job).
probe="journey-manager-path-probe-${GITHUB_RUN_ID:-0}-${GITHUB_RUN_ATTEMPT:-0}.service"
mkdir -p "$probe_dir"
printf '[Unit]\nDescription=Journey unit search path probe\n[Service]\nType=oneshot\nExecStart=/usr/bin/true\n' > "$probe_dir/$probe"
timeout 10s systemctl --user daemon-reload
test "$(timeout 10s systemctl --user show "$probe" --property=FragmentPath --value)" = "$probe_dir/$probe"
timeout 30s systemctl --user start "$probe"
rm "$probe_dir/$probe"
timeout 10s systemctl --user daemon-reload
rmdir "$probe_dir" "$config/systemd" "$config" "$run_root/home" "$run_root"
printf 'XDG_RUNTIME_DIR=%s\nDBUS_SESSION_BUS_ADDRESS=%s\n' "$XDG_RUNTIME_DIR" "$DBUS_SESSION_BUS_ADDRESS" >> "$GITHUB_ENV"
echo 'Isolated systemd user manager is ready.'
