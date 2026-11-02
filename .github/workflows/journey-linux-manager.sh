#!/usr/bin/env bash
# Hosted direct Linux row only. Keep diagnostics outside the driver's fresh root.
set -Eeuo pipefail
diag="$RUNNER_TEMP/journey-manager-direct-linux"
mkdir -p "$diag"
exec > >(tee "$diag/prerequisite.log") 2>&1
uid=$(id -u)
export XDG_RUNTIME_DIR="/run/user/$uid"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"

diagnose() {
  local rc=$?
  trap - EXIT
  echo "setup_exit=$rc"
  timeout 10s loginctl show-user "$uid" -p Name -p Linger -p State -p RuntimePath || true
  timeout 10s sudo systemctl --no-pager --full status "user@$uid.service" | head -60 || true
  timeout 10s systemctl --user show -p Version -p UnitPath -p NFailedUnits || true
  timeout 10s systemctl --user --no-pager --full status | head -60 || true
  exit "$rc"
}
trap diagnose EXIT

echo "uid=$uid runtime=$XDG_RUNTIME_DIR"
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
# Prove the native manager can register and execute a transient user service.
timeout 30s systemd-run --user --wait --collect --unit="journey-manager-probe-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" /usr/bin/true
printf 'XDG_RUNTIME_DIR=%s\nDBUS_SESSION_BUS_ADDRESS=%s\n' "$XDG_RUNTIME_DIR" "$DBUS_SESSION_BUS_ADDRESS" >> "$GITHUB_ENV"
echo 'Native systemd user manager ready.'
