#!/usr/bin/env bash
# Disposable GitHub-hosted ubuntu-24.04 VM only; called from workspace root.
set -Eeuo pipefail
out="$PWD/l2-results"
mkdir -p "$out"
exec > >(tee "$out/console.log") 2>&1
uid=$(id -u)
run_root="${RUNNER_TEMP:?}/astrid-l2-real-linux"
config="$run_root/home/.config"
unit_dir="$config/systemd/user"
dropin="/run/systemd/system/user@$uid.service.d/90-astrid-l2.conf"
export XDG_RUNTIME_DIR="/run/user/$uid"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
setup_owned=0
cleanup() {
  rc=$?
  trap - EXIT
  set +e
  echo "test_or_setup_exit=$rc"
  cleanup_rc=0
  if (( setup_owned )); then
    timeout 10s systemctl --user --no-pager --full status astrid.service > "$out/service-before-cleanup.txt" 2>&1 || true
    timeout 10s journalctl --user -u astrid.service --no-pager -n 300 > "$out/service-journal.txt" 2>&1 || true
    if [[ -d "$run_root" ]]; then
      timeout 20s find "$run_root" -maxdepth 8 -type f \( -name 'runtime-service.log' -o -name 'install.log' -o -name 'astrid-install.json' \) -print0 | head -z -n 8 | while IFS= read -r -d '' file; do
        name=$(printf '%s' "$file" | sha256sum | cut -c1-16)
        printf 'source=%s\n' "$file" > "$out/log-$name.txt"
        tail -c 1048576 "$file" >> "$out/log-$name.txt"
      done
    fi
    fragment=$(timeout 10s systemctl --user show astrid.service -p FragmentPath --value 2>/dev/null)
    if [[ "$fragment" == "$unit_dir/astrid.service" ]]; then
      timeout 30s systemctl --user disable --now astrid.service || cleanup_rc=1
      rm -f "$unit_dir/astrid.service" || cleanup_rc=1
      timeout 10s systemctl --user daemon-reload || cleanup_rc=1
    elif [[ -z "$fragment" && -f "$unit_dir/astrid.service" ]]; then
      rm -f "$unit_dir/astrid.service" || cleanup_rc=1
      timeout 10s systemctl --user daemon-reload || cleanup_rc=1
    elif [[ -n "$fragment" && "$fragment" != /dev/null ]]; then
      echo "Refusing to touch unowned unit: $fragment"
      cleanup_rc=1
    fi
    sudo rm -f "$dropin" || cleanup_rc=1
    sudo systemctl daemon-reload || cleanup_rc=1
    timeout 30s sudo systemctl restart "user@$uid.service" || cleanup_rc=1
    if [[ "$linger_before" == no ]]; then timeout 20s sudo loginctl disable-linger "$uid" || cleanup_rc=1; fi
  fi
  printf '%s\n' "$cleanup_rc" > "$out/cleanup-exit.txt"
  printf '%s\n' "$rc" > "$out/test-exit.txt"
  if (( cleanup_rc != 0 && rc == 0 )); then rc=1; fi
  printf '%s\n' "$rc" > "$out/combined-exit.txt"
  exit "$rc"
}
trap cleanup EXIT
[[ "${GITHUB_ACTIONS:-}" == true && "$uid" -ne 0 && "${RUNNER_OS:-}" == Linux ]]
[[ "$run_root" =~ ^/[a-zA-Z0-9_./-]+$ ]]
test ! -e "$run_root"
test ! -e "$dropin"
echo "uid=$uid runner_temp=$RUNNER_TEMP run_root=$run_root"
uname -a
systemctl --version | head -1
systemd_version=$(systemctl --version | sed -n '1s/^systemd \([0-9][0-9]*\).*/\1/p')
[[ "$systemd_version" =~ ^[0-9]+$ ]] && (( systemd_version >= 254 ))
linger_before=$(loginctl show-user "$uid" -p Linger --value)
[[ "$linger_before" == yes || "$linger_before" == no ]]
timeout 20s sudo loginctl enable-linger "$uid"
setup_owned=1
timeout 30s sudo systemctl start "user@$uid.service"
for attempt in {1..20}; do
  if [[ -S "$XDG_RUNTIME_DIR/bus" ]] && timeout 3s systemctl --user show-environment >/dev/null; then break; fi
  sleep 1
done
test "$(stat -c %u "$XDG_RUNTIME_DIR")" = "$uid"
test -S "$XDG_RUNTIME_DIR/bus"
timeout 10s systemctl --user show-environment >/dev/null
test ! -e "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/astrid.service"
test "$(timeout 10s systemctl --user show astrid.service -p LoadState --value)" = not-found
sudo mkdir -p "$(dirname "$dropin")"
printf '[Service]\nEnvironment="XDG_CONFIG_HOME=%s" "XDG_DATA_HOME=%s" "SYSTEMD_UNIT_PATH=%s:"\n' "$config" "$run_root/home/.local/share" "$unit_dir" | sudo tee "$dropin" >/dev/null
sudo systemctl daemon-reload
timeout 30s sudo systemctl restart "user@$uid.service"
for attempt in {1..20}; do
  if timeout 3s systemctl --user show-environment >/dev/null; then break; fi
  sleep 1
done
mkdir -p "$unit_dir"
probe="l2-path-probe-${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}.service"
printf '[Unit]\nDescription=L2 isolated search path probe\n[Service]\nType=oneshot\nExecStart=/usr/bin/true\n' > "$unit_dir/$probe"
timeout 10s systemctl --user daemon-reload
test "$(timeout 10s systemctl --user show "$probe" -p FragmentPath --value)" = "$unit_dir/$probe"
timeout 30s systemctl --user start "$probe"
rm "$unit_dir/$probe"
timeout 10s systemctl --user daemon-reload
rmdir "$unit_dir" "$config/systemd" "$config" "$run_root/home" "$run_root"
timeout 45m python3 -u agent/scripts/l2_real_linux.py --app "$PWD/app" --run-root "$run_root" --output "$out"
