#!/bin/sh
# Install a pinned NVIDIA OpenShell gateway for running Astrid in a sandbox.
#
# No sudo, no system packages, no login service: the binaries, config, PKI,
# gateway database and VM images all live in one user-owned folder, and the
# CLI's client certificate goes in ~/.config/openshell/gateways/<name>/mtls.
# Re-running is safe; finished steps are kept.
#
# Tested: Linux x86_64 (Ubuntu, kernel 7.0) with the microVM (libkrun) driver.
# UNTESTED: Linux arm64 (same steps, different asset names), WSL 2 (needs
# /dev/kvm, i.e. nested virtualisation). macOS: use OpenShell's Homebrew
# package instead; see docs/setup/openshell-sandbox.md.
#
# Settings (environment):
#   ASTRID_OPENSHELL_HOME     install folder   (default ${XDG_DATA_HOME:-~/.local/share}/astrid-openshell)
#   ASTRID_OPENSHELL_GATEWAY  CLI gateway name (default astrid)
#   ASTRID_OPENSHELL_PORT     gateway port on 127.0.0.1 (default 17670)
#   ASTRID_OPENSHELL_VCPUS / _MEM_MIB / _DISK_MIB  per-sandbox VM size (4 / 8192 / 32768)
#   ASTRID_OPENSHELL_HOST_MOUNTS=1  allow read-only host folder shares (for the
#                             "ComfyUI on this machine" service). OpenShell calls
#                             this an unsafe operator override: libkrun does not
#                             confine a compromised guest kernel to the shared
#                             folder. Off by default.
set -eu
umask 077

OPENSHELL_VERSION=0.1.5
home=${ASTRID_OPENSHELL_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/astrid-openshell}
name=${ASTRID_OPENSHELL_GATEWAY:-astrid}
port=${ASTRID_OPENSHELL_PORT:-17670}
vcpus=${ASTRID_OPENSHELL_VCPUS:-4}
mem_mib=${ASTRID_OPENSHELL_MEM_MIB:-8192}
disk_mib=${ASTRID_OPENSHELL_DISK_MIB:-32768}
host_mounts=${ASTRID_OPENSHELL_HOST_MOUNTS:-0}

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

case "$name" in *[!A-Za-z0-9_-]*|'') die "ASTRID_OPENSHELL_GATEWAY must be letters, digits, - or _." ;; esac
case "$home" in /*) ;; *) die "ASTRID_OPENSHELL_HOME must be an absolute path." ;; esac

# --- platform -------------------------------------------------------------
case "$(uname -s)" in
    Linux) ;;
    Darwin) die "On macOS install OpenShell with Homebrew (UNTESTED with Astrid); see docs/setup/openshell-sandbox.md." ;;
    *) die "Unsupported OS $(uname -s). On Windows, run this inside WSL 2 (UNTESTED)." ;;
esac
case "$(uname -m)" in
    x86_64|amd64) arch=x86_64 ;;
    aarch64|arm64) arch=aarch64; say "note: Linux arm64 is UNTESTED with this script." ;;
    *) die "Unsupported CPU $(uname -m)." ;;
esac
if grep -qi microsoft /proc/version 2>/dev/null; then
    say "note: WSL 2 detected. OpenShell calls WSL 2 experimental and this script is UNTESTED there."
fi

# The microVM driver gives each sandbox its own kernel. It needs /dev/kvm.
if [ ! -r /dev/kvm ] || [ ! -w /dev/kvm ]; then
    die "/dev/kvm is missing or not usable by $(id -un). Enable virtualisation, then add yourself to the kvm group (sudo usermod -aG kvm $(id -un)) and log in again."
fi
for tool in curl tar sha256sum mke2fs debugfs e2fsck; do
    command -v "$tool" >/dev/null 2>&1 || PATH=$PATH:/usr/sbin:/sbin
done
for tool in curl tar sha256sum mke2fs debugfs e2fsck; do
    command -v "$tool" >/dev/null 2>&1 || die "missing $tool (mke2fs/debugfs/e2fsck come from e2fsprogs)."
done

# --- pinned release assets (sha256 from the v0.1.5 release checksum files) --
assets() {
    case "$arch" in
        x86_64) cat <<'EOF'
openshell-x86_64-unknown-linux-musl.tar.gz ca1ad6e6fbb4a6cd407e95763391b8ecff02912e7d9c467e7ca22696fe80af69
openshell-gateway-x86_64-unknown-linux-gnu.tar.gz 6ed6a6aff420c65250082cc1db48dc6271a6f5eec0e0068133566ffae4aa788b
openshell-driver-vm-x86_64-unknown-linux-gnu.tar.gz 6465e39666276215a9fa05d27991e67c9c88267284b59aca95cb7dbb9d69cd41
openshell-supervisor-x86_64-unknown-linux-gnu.tar.gz 42184d30c1bfb4503954908d0cd629eae6b6203c331c51b6319034e37dec9e59
openshell-sandbox-x86_64-unknown-linux-musl.tar.gz e7a67e0254a19b829171d5bb1b5c9e00a3ea98411d941c9c99ad6d8fd7e181b5
openshell-prover-x86_64-unknown-linux-musl.tar.gz 79e6c13db4e6cb819afc90b3d7ca8c920875e4a082cffb1984e9656a12670914
EOF
        ;;
        aarch64) cat <<'EOF'
openshell-aarch64-unknown-linux-musl.tar.gz 30c949b4ac07a9c8245c1248eb82e4eb241615134fcabf3c9bf30dec7d00addf
openshell-gateway-aarch64-unknown-linux-gnu.tar.gz 125f9f43b8d06da8280c78089904a6da2faa7c4cb03f82db3b31fc980cf6c62d
openshell-driver-vm-aarch64-unknown-linux-gnu.tar.gz fc49a0bebfda3075a996123c0fd252200e2987856da63fbd8e7f021f327e1e68
openshell-supervisor-aarch64-unknown-linux-gnu.tar.gz 70995e9d9c8e7d314bdb17ef3fd48ef02f27b7922cdf2fda1ee10963441ac8dd
openshell-sandbox-aarch64-unknown-linux-musl.tar.gz 5d9b633c2f2b935af0193089828a38b88bce74c1c94e7eab69329c86130dbcfb
openshell-prover-aarch64-unknown-linux-musl.tar.gz 2d7e9a1fadfdccee3a49a25a84660d7eddcbcbb3af59ab9068442d8770fdcf2f
EOF
        ;;
    esac
}

say "Installing OpenShell $OPENSHELL_VERSION into $home"
mkdir -p "$home/dl/v$OPENSHELL_VERSION" "$home/bin" "$home/config" "$home/logs" "$home/vm" "$home/state/tls"
chmod 700 "$home/state" "$home/state/tls" "$home/vm"
dl="$home/dl/v$OPENSHELL_VERSION"
base="https://github.com/NVIDIA/OpenShell/releases/download/v$OPENSHELL_VERSION"

assets | while read -r asset sum; do
    if [ ! -f "$dl/$asset" ] || [ "$(sha256sum "$dl/$asset" | cut -d' ' -f1)" != "$sum" ]; then
        say "  downloading $asset"
        curl --fail --silent --show-error --location "$base/$asset" --output "$dl/$asset.part"
        mv "$dl/$asset.part" "$dl/$asset"
    fi
    [ "$(sha256sum "$dl/$asset" | cut -d' ' -f1)" = "$sum" ] || die "checksum mismatch for $asset"
    tar -xzf "$dl/$asset" -C "$home/bin"
done
chmod 0755 "$home/bin"/*
if command -v gh >/dev/null 2>&1 && [ "${ASTRID_OPENSHELL_ATTEST:-0}" = 1 ]; then
    # Optional: check the GitHub build attestations as well as the hashes.
    assets | while read -r asset sum; do
        gh attestation verify "$dl/$asset" -R NVIDIA/OpenShell --format json >/dev/null \
            || die "attestation check failed for $asset"
    done
    say "  attestations verified"
fi
"$home/bin/openshell" --version

# --- PKI --------------------------------------------------------------------
# generate-certs also copies client certificates to the CLI's default
# gateway folder (~/.config/openshell/gateways/openshell/mtls), overwriting
# any existing OpenShell setup. Point it at a throwaway config dir and
# install the client certificate under our own gateway name instead.
if [ ! -f "$home/state/tls/client/tls.key" ]; then
    scratch=$(mktemp -d)
    XDG_CONFIG_HOME=$scratch "$home/bin/openshell-gateway" generate-certs --output-dir "$home/state/tls" >/dev/null
    rm -rf "$scratch"
fi
cli_dir="${XDG_CONFIG_HOME:-$HOME/.config}/openshell/gateways/$name/mtls"
mkdir -p "$cli_dir"
chmod 700 "$cli_dir"
cp "$home/state/tls/ca.crt" "$cli_dir/ca.crt"
cp "$home/state/tls/client/tls.crt" "$cli_dir/tls.crt"
cp "$home/state/tls/client/tls.key" "$cli_dir/tls.key"
chmod 600 "$cli_dir/tls.key"

# --- config -----------------------------------------------------------------
# Loopback only, mTLS, no unauthenticated users, no host bind mounts, policy
# validation fails closed. See OpenShell docs/how-it-works/gateways/configuration.mdx.
cat >"$home/config/gateway.toml" <<EOF
# Written by astrid-agent scripts/openshell/install-openshell.sh ($OPENSHELL_VERSION)
[openshell]
version = 2

[openshell.gateway]
name = "$name"
bind_address = "127.0.0.1:$port"
log_level = "info"
compute_driver = "vm"
guest_tls_ca = "$home/state/tls/ca.crt"
disable_tls = false
enable_websocket_tunnel = false
policy_validation_failure_mode = "fail_closed"

[openshell.gateway.tls]
cert_path = "$home/state/tls/server/tls.crt"
key_path = "$home/state/tls/server/tls.key"
client_ca_path = "$home/state/tls/ca.crt"

[openshell.gateway.gateway_jwt]
signing_key_path = "$home/state/tls/jwt/signing.pem"
public_key_path = "$home/state/tls/jwt/public.pem"
kid_path = "$home/state/tls/jwt/kid"
gateway_id = "$name"

[openshell.gateway.auth]
allow_unauthenticated_users = false

[openshell.drivers.vm]
state_dir = "$home/vm"
driver_dir = "$home/bin"
default_image = "nvcr.io/nvidia/base/ubuntu:24.04"
bootstrap_image = "nvcr.io/nvidia/base/ubuntu:24.04"
krun_log_level = 1
vcpus = $vcpus
mem_mib = $mem_mib
overlay_disk_mib = $disk_mib
EOF
if [ "$host_mounts" = 1 ]; then
    cat >>"$home/config/gateway.toml" <<'EOF'
# ASTRID_OPENSHELL_HOST_MOUNTS=1: read-only host folder shares (virtiofs).
# OpenShell's "unsafe operator override"; see docs/setup/openshell-sandbox.md.
allow_driver_config = true
enable_bind_mounts = true

[openshell.drivers.vm.resource_admission]
enabled = false
EOF
else
    echo "enable_bind_mounts = false" >>"$home/config/gateway.toml"
fi

cat >"$home/gateway.env" <<EOF
OPENSHELL_GATEWAY_CONFIG=$home/config/gateway.toml
OPENSHELL_TELEMETRY_ENABLED=false
# Gateway SQLite DB lands at \$XDG_STATE_HOME/openshell/gateway/openshell.db
XDG_STATE_HOME=$home/state
EOF

cat >"$home/start-gateway.sh" <<'EOF'
#!/bin/sh
# Start this OpenShell gateway in the background (it does not start at login).
set -eu
O=$(cd "$(dirname "$0")" && pwd)
set -a
. "$O/gateway.env"
set +a
if [ -f "$O/state/gateway.pid" ] && kill -0 "$(cat "$O/state/gateway.pid")" 2>/dev/null; then
    echo "already running: pid $(cat "$O/state/gateway.pid")"
    exit 0
fi
"$O/bin/openshell-gateway" config preflight
setsid nohup "$O/bin/openshell-gateway" >>"$O/logs/gateway.log" 2>&1 </dev/null &
echo $! >"$O/state/gateway.pid"
echo "started: pid $(cat "$O/state/gateway.pid"), log $O/logs/gateway.log"
EOF
cat >"$home/stop-gateway.sh" <<'EOF'
#!/bin/sh
# Stop this OpenShell gateway by its recorded pid.
set -eu
O=$(cd "$(dirname "$0")" && pwd)
pid=$(cat "$O/state/gateway.pid" 2>/dev/null) || { echo "not running"; exit 0; }
kill "$pid" 2>/dev/null && echo "stopped pid $pid" || echo "not running"
rm -f "$O/state/gateway.pid"
EOF
chmod 0755 "$home/start-gateway.sh" "$home/stop-gateway.sh"

# --- start and register -----------------------------------------------------
# A running gateway keeps its old config; restart it if the config changed.
if [ -f "$home/state/gateway.pid" ] && kill -0 "$(cat "$home/state/gateway.pid")" 2>/dev/null \
        && ! cmp -s "$home/config/gateway.toml" "$home/state/gateway.toml.running" 2>/dev/null; then
    say "Gateway config changed; restarting the gateway (running sandboxes stop)."
    "$home/stop-gateway.sh"
    sleep 2
fi
"$home/start-gateway.sh"
cp "$home/config/gateway.toml" "$home/state/gateway.toml.running"
# Wait for the listener (curl exit 7 = nothing listening; mTLS refusals are fine).
tries=0
while :; do
    rc=0
    curl --silent --insecure --max-time 2 --output /dev/null "https://127.0.0.1:$port/" || rc=$?
    [ "$rc" -ne 7 ] && break
    tries=$((tries + 1))
    [ "$tries" -le 30 ] || die "gateway did not start; see $home/logs/gateway.log"
    sleep 1
done
if ! "$home/bin/openshell" gateway list 2>/dev/null | grep -qw "$name"; then
    # `gateway add` makes the new gateway the active one; keep the user's
    # existing choice (our own scripts always pass -g explicitly).
    active="${XDG_CONFIG_HOME:-$HOME/.config}/openshell/active_gateway"
    previous=$(cat "$active" 2>/dev/null || true)
    "$home/bin/openshell" gateway add "https://127.0.0.1:$port" --local --name "$name"
    if [ -n "$previous" ] && [ "$previous" != "$name" ]; then
        "$home/bin/openshell" gateway select "$previous" >/dev/null
    fi
fi
"$home/bin/openshell" -g "$name" status

say ""
say "OpenShell is running at https://127.0.0.1:$port (gateway '$name', telemetry off)."
say "It does not start at login. After a reboot run: $home/start-gateway.sh"
say "Next: ASTRID_OPENSHELL_HOME=$home scripts/openshell/astrid-openshell.sh up"
