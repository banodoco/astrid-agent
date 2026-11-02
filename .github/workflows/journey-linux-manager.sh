#!/usr/bin/env bash
# Hosted direct Linux row only. Keep diagnostics outside the driver's fresh root.
set -Eeuo pipefail
diag="$RUNNER_TEMP/journey-manager-direct-linux"
mkdir -p "$diag"
exec > >(tee "$diag/prerequisite.log") 2>&1
uid=$(id -u)
export XDG_RUNTIME_DIR="/run/user/$uid"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
run_root="$RUNNER_TEMP/astrid-r1-direct-linux"
config="$run_root/home/.config"
data="$run_root/home/.local/share"
probe="journey-manager-path-probe-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.service"
probe_dir="$config/systemd/user"
probe_created=0

diagnose() {
  local rc=$?
  trap - EXIT
  if (( probe_created )); then
    rm -f "$probe_dir/$probe"
    systemctl --user daemon-reload || true
    rmdir "$probe_dir" "$config/systemd" "$config" "$run_root/home" "$run_root" || true
  fi
  echo "setup_exit=$rc"
  timeout 10s loginctl show-user "$uid" -p Name -p Linger -p State -p RuntimePath || true
  timeout 10s sudo systemctl --no-pager --full status "user@$uid.service" | head -60 || true
  timeout 10s systemctl --user show -p Version -p UnitPath -p NFailedUnits || true
  timeout 10s systemctl --user show-environment || true
  exit "$rc"
}
trap diagnose EXIT

probe_isolated_unit() {
  # Prove discovery from the exact future installation directory, not merely the bus.
  mkdir -p "$probe_dir"
  probe_created=1
  printf '[Unit]\nDescription=Journey unit search path probe\n[Service]\nType=oneshot\nExecStart=/usr/bin/true\n' > "$probe_dir/$probe"
  timeout 10s systemctl --user daemon-reload
  fragment=$(timeout 10s systemctl --user show "$probe" --property=FragmentPath --value)
  test "$fragment" = "$probe_dir/$probe"
  timeout 30s systemctl --user start "$probe"
  rm "$probe_dir/$probe"
  timeout 10s systemctl --user daemon-reload
  rmdir "$probe_dir" "$config/systemd" "$config" "$run_root/home" "$run_root"
  probe_created=0
}

# The runtime drop-in is interpreted by systemd, so restrict it to the ordinary
# GitHub runner temp path alphabet rather than accepting systemd specifiers.
[[ "$run_root" =~ ^/[a-zA-Z0-9_./-]+$ ]]
test ! -e "$run_root"
echo "uid=$uid runtime=$XDG_RUNTIME_DIR isolated_config=$config isolated_data=$data"
timeout 20s sudo loginctl enable-linger "$uid"
timeout 30s sudo systemctl start "user@$uid.service"
for attempt in {1..20}; do
  if [[ -d "$XDG_RUNTIME_DIR" && -S "$XDG_RUNTIME_DIR/bus" ]] && \
     timeout 3s systemctl --user show-environment >/dev/null; then
    break
  fi
  sleep 1
done
test "$(stat -c %u "$XDG_RUNTIME_DIR")" = "$uid"
test -S "$XDG_RUNTIME_DIR/bus"
timeout 10s systemctl --user show-environment >/dev/null
# Check the original manager before restarting it with a different search path.
test ! -e "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/astrid.service"
original=$(timeout 10s systemctl --user show astrid.service --property=LoadState --value || true)
test "$original" = not-found

dropin="/run/systemd/system/user@$uid.service.d"
sudo mkdir -p "$dropin"
# An explicit unit search path survives the fresh-root cleanup between this
# probe and the installer. The trailing colon retains systemd's default paths.
printf '[Service]\nEnvironment="XDG_CONFIG_HOME=%s" "XDG_DATA_HOME=%s" "SYSTEMD_UNIT_PATH=%s:"\n' "$config" "$data" "$probe_dir" | sudo tee "$dropin/90-astrid-journey.conf" >/dev/null
sudo systemctl daemon-reload
timeout 30s sudo systemctl restart "user@$uid.service"
for attempt in {1..20}; do
  if timeout 3s systemctl --user show-environment >/dev/null; then break; fi
  sleep 1
done
probe_isolated_unit
printf 'XDG_RUNTIME_DIR=%s\nDBUS_SESSION_BUS_ADDRESS=%s\n' "$XDG_RUNTIME_DIR" "$DBUS_SESSION_BUS_ADDRESS" >> "$GITHUB_ENV"
echo 'Native systemd user manager discovered and started the isolated-path probe.'
