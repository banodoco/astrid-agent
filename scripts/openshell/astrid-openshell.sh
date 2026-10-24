#!/bin/sh
# Run Astrid inside an NVIDIA OpenShell microVM sandbox instead of your own
# account. The sandbox has its own kernel, sees none of your files, and gets
# network access only while installing (package and release hosts, read-only)
# and afterwards only to an LLM API you attach. The browser door is forwarded
# to the same 127.0.0.1:17340 that astrid.haus expects.
#
#   scripts/openshell/install-openshell.sh          # once: pinned gateway, no sudo
#   scripts/openshell/astrid-openshell.sh fetch      # download the installer to review
#   scripts/openshell/astrid-openshell.sh up DIR     # create, install, lock down, check, door
#
# Commands:
#   fetch [DIR]        download https://astrid.haus/install + manifest into DIR, print hashes and pins
#   create             create the sandbox (install-time policy)
#   install DIR        run DIR/install.sh with DIR/install-manifest.json inside the sandbox
#   lockdown           switch to the run-time policy (no network)
#   start              (re)start Astrid's Runtime inside the sandbox
#   door               forward 127.0.0.1:17340 on this machine to the sandbox's door
#   check              prove the live policy is inside policies/boundary.yaml, then probe egress
#   provider NAME      attach an LLM key as an OpenShell provider (anthropic, openai, openrouter)
#   status | shell | logs
#   stop | resume      stop the VM keeping its disk, or start it again (then: start, door)
#   destroy            delete the sandbox and its disk (asks first)
#   up DIR             create + install DIR + lockdown + start + check + door
#
# Settings (environment):
#   ASTRID_OPENSHELL_HOME     where install-openshell.sh put OpenShell
#   ASTRID_OPENSHELL_GATEWAY  gateway name (default astrid)
#   ASTRID_SANDBOX            sandbox name (default astrid)
#   ASTRID_SANDBOX_IMAGE      sandbox image (default: buildpack-deps noble-scm, pinned by digest)
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
home=${ASTRID_OPENSHELL_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/astrid-openshell}
gateway=${ASTRID_OPENSHELL_GATEWAY:-astrid}
sandbox=${ASTRID_SANDBOX:-astrid}
# Ubuntu 24.04 with curl, wget, git and CA certificates: what Astrid's
# installer needs. The default OpenShell image has no curl or wget.
image=${ASTRID_SANDBOX_IMAGE:-docker.io/library/buildpack-deps:noble-scm@sha256:59d4f4d8581d741bb2c31f4bfd00a557ca94d182d082c81bd56d32afbad8dfc9}
door_port=17340   # Astrid's door checks Host: 127.0.0.1:17340 exactly
guest_home=/sandbox
stage_name=astrid-installer

if [ -x "$home/bin/openshell" ]; then os_cli="$home/bin/openshell"; else os_cli=openshell; fi
if [ -x "$home/bin/openshell-prover" ]; then prover="$home/bin/openshell-prover"; else prover=openshell-prover; fi

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
osh() { "$os_cli" -g "$gateway" "$@"; }
in_sandbox() { osh sandbox exec -n "$sandbox" --no-tty -- sh -c "$1"; }
need_python() { command -v python3 >/dev/null 2>&1 || die "python3 is needed on this machine for JSON handling."; }

# --- commands -----------------------------------------------------------------

cmd_fetch() {
    dir=${1:-$home/installers/$(date -u +%Y-%m-%dT%H%M%SZ)}
    mkdir -p "$dir"
    curl --fail --silent --show-error --location https://astrid.haus/install --output "$dir/install.sh"
    curl --fail --silent --show-error --location https://astrid.haus/install-manifest.json --output "$dir/install-manifest.json"
    need_python
    say "Saved to $dir"
    (cd "$dir" && sha256sum install.sh install-manifest.json)
    python3 -I - "$dir/install-manifest.json" <<'EOF'
import json, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
print("agent   ", m["agent"]["sha"])
print("runtime ", m["runtime"]["sha"])
omp = m["tools"]["omp"].get("source")
print("omp     ", omp if isinstance(omp, str) else f'{omp["repo"]} {omp["sha"]}')
EOF
    say "Review these files before installing: astrid-openshell.sh up $dir"
}

cmd_create() {
    if osh sandbox get "$sandbox" >/dev/null 2>&1; then
        say "Sandbox '$sandbox' already exists."
        return
    fi
    # --no-auto-providers: never copy credentials from this shell into the
    # sandbox. `sleep infinity` keeps the VM up; Runtime runs beside it.
    osh sandbox create --name "$sandbox" --from "$image" \
        --policy "$here/policies/install.yaml" --no-auto-providers --no-tty \
        --label app=astrid --detach -- sleep infinity
}

# Copy the installer (and any local Git sources its manifest names) into the
# sandbox, rewriting local `repo` paths to their copies inside the sandbox.
stage_installer() {
    src=$1
    [ -f "$src/install.sh" ] && [ -f "$src/install-manifest.json" ] \
        || die "$src must contain install.sh and install-manifest.json (see: fetch)."
    need_python
    staging=$(mktemp -d)
    trap 'rm -rf "$staging"' EXIT
    mkdir -p "$staging/$stage_name/sources"
    cp "$src/install.sh" "$staging/$stage_name/install.sh"
    python3 -I - "$src/install-manifest.json" "$staging/$stage_name" "$guest_home/$stage_name" <<'EOF'
import json, subprocess, sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname
manifest, staging, guest = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
pins = json.loads(manifest.read_text(encoding="utf-8"))
for name in ("agent", "runtime"):
    repo = pins[name].get("repo")
    if not repo:
        continue
    if repo.startswith("file://"):
        repo = url2pathname(urlsplit(repo).path)
    # A bare single-commit clone is enough for the installer's `git archive`.
    target = staging / "sources" / f"{name}.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", "--no-local", repo, str(target)], check=True)
    subprocess.run(["git", "--git-dir", str(target), "cat-file", "-e", pins[name]["sha"] + "^{commit}"], check=True)
    pins[name]["repo"] = f"{guest}/sources/{name}.git"
    print(f"  {name}: local source {repo} @ {pins[name]['sha']}")
(staging / "install-manifest.json").write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8")
EOF
    in_sandbox "rm -rf '$guest_home/$stage_name'"
    osh sandbox upload "$sandbox" "$staging/$stage_name" "$guest_home" >/dev/null
    rm -rf "$staging"
    trap - EXIT
}

cmd_install() {
    [ $# -ge 1 ] || die "usage: install DIR (from: fetch)"
    say "Install-time policy: read-only access to package and release hosts."
    osh policy set "$sandbox" --policy "$here/policies/install.yaml" --wait >/dev/null
    stage_installer "$1"
    say "Running Astrid's installer inside '$sandbox' (several minutes the first time)."
    in_sandbox "cd '$guest_home/$stage_name' && sha256sum install.sh install-manifest.json && ASTRID_INSTALL_MANIFEST_URL='file://$guest_home/$stage_name/install-manifest.json' sh ./install.sh"
}

cmd_lockdown() {
    osh policy set "$sandbox" --policy "$here/policies/run.yaml" --wait >/dev/null
    say "Run-time policy active: no network except attached LLM providers."
}

door_answers() {
    in_sandbox "curl --silent --max-time 3 --output /dev/null --write-out '%{http_code}' -H 'Origin: https://astrid.haus' http://127.0.0.1:$door_port/" 2>/dev/null \
        | grep -qE '^(200|403|404)$'
}

cmd_start() {
    if door_answers; then
        say "Runtime is already running (door answers inside the sandbox)."
        return
    fi
    # Without systemd, Astrid's documented restart is the install line, which
    # re-bootstraps from the network the run-time policy blocks. Start Runtime
    # the way the installer's no-systemd fallback does. Stopping a running
    # Runtime is not possible from here: OpenShell's seccomp filter denies
    # pidfds and Runtime refuses to signal a bare PID, so restart = stop +
    # resume the VM, then start.
    in_sandbox "cd '$guest_home/.local/share/astrid' && V=versions/\$(cat current) && \"\$V/py/bin/python\" -I -c '
from pathlib import Path
from astrid.installer.core import current
from astrid.installer.service import Service
root = Path(\"$guest_home/.local/share/astrid\")
log = Path(\"$guest_home/.local/state/astrid-runtime/logs/runtime-service.log\")
Service(root, log, None).start_foreground(current(root), Path(\"$guest_home/.local/share/astrid-runtime\"))
print(\"Runtime starting; log:\", log)
'"
    tries=0
    until door_answers; do
        tries=$((tries + 1))
        [ "$tries" -le 60 ] || die "Runtime did not open its door; see $guest_home/.local/state/astrid-runtime/logs/ (astrid-openshell.sh shell)"
        sleep 2
    done
    say "Runtime is up."
}

cmd_door() {
    if osh forward list 2>/dev/null | grep -w "$sandbox" | grep -q "$door_port"; then
        say "Door already forwarded."
    else
        osh forward start "127.0.0.1:$door_port" "$sandbox" --background
    fi
    code=$(curl --silent --output /dev/null --write-out '%{http_code}' -H 'Origin: https://astrid.haus' "http://127.0.0.1:$door_port/" || true)
    case "$code" in
        403|404|200) say "Door: http://127.0.0.1:$door_port (answering, HTTP $code). Open https://astrid.haus in a separate browser profile." ;;
        *) say "Door forwarded but not answering yet (HTTP $code). Try: astrid-openshell.sh start" ;;
    esac
}

cmd_check() {
    need_python
    live=$(mktemp)
    osh policy get "$sandbox" --full -o json | python3 -I -c '
import json, sys
policy = json.load(sys.stdin)["policy"]
policy["version"] = 1
json.dump(policy, sys.stdout)' >"$live"
    result=$("$prover" check --boundary "$here/policies/boundary.yaml" "$live" || true)
    rm -f "$live"
    printf '%s\n' "$result"
    # The prover exits 0 for every verdict; only this line means "proved".
    printf '%s\n' "$result" | grep -q '^result: within_boundary$' || die "live policy is NOT inside policies/boundary.yaml"

    say "Probing egress from inside the sandbox (all must be blocked):"
    in_sandbox '
fail=0
for url in https://example.com https://github.com https://astrid.haus \
           http://host.openshell.internal:8188 http://host.openshell.internal:8000 \
           https://host.openshell.internal:17670 http://169.254.169.254/ http://10.0.0.1/ http://192.168.1.1/; do
    if curl --silent --max-time 5 --output /dev/null "$url"; then
        echo "  REACHABLE  $url"; fail=1
    else
        echo "  blocked    $url"
    fi
done
for path in /mnt /home /root /var/run/docker.sock /run/docker.sock; do
    [ -e "$path" ] && [ -n "$(ls -A "$path" 2>/dev/null)" ] && { echo "  VISIBLE    $path"; fail=1; } || echo "  absent     $path"
done
exit $fail' || die "an egress or host-path probe was NOT blocked"
    say "Check passed."
}

cmd_provider() {
    [ $# -ge 1 ] || die "usage: provider anthropic|openai|openrouter"
    profile="$here/providers/astrid-$1.yaml"
    [ -f "$profile" ] || die "no profile for '$1' (have: anthropic, openai, openrouter)"
    env_var=$(sed -n 's/^ *env_vars: \[\([A-Z_]*\)\]/\1/p' "$profile")
    eval "value=\${$env_var:-}"
    [ -n "$value" ] || die "export $env_var first (a throwaway key with a spend cap). It is read from your environment, never put on a command line."
    osh profile import -f "$profile" >/dev/null 2>&1 || osh profile update -f "$profile" "astrid-$1" >/dev/null
    if osh provider get "astrid-$1" >/dev/null 2>&1; then
        osh provider update "astrid-$1" --credential "$env_var" >/dev/null
    else
        osh provider create --name "astrid-$1" --type "astrid-$1" --credential "$env_var" >/dev/null
    fi
    osh sandbox provider attach "$sandbox" "astrid-$1" --wait
    say "Attached. Inside the sandbox $env_var is a placeholder; OpenShell's proxy adds the real key."
}

cmd_destroy() {
    printf "Delete sandbox '%s' and everything in it? [y/N] " "$sandbox"
    read -r answer
    [ "$answer" = y ] || [ "$answer" = Y ] || { say "Kept."; return; }
    osh forward stop "$door_port" "$sandbox" >/dev/null 2>&1 || true
    osh sandbox delete "$sandbox"
}

cmd_up() {
    [ $# -ge 1 ] || die "usage: up DIR (from: fetch)"
    cmd_create
    cmd_install "$1"
    # Network rules are live, so the Runtime the installer started is now
    # under the run-time policy too.
    cmd_lockdown
    cmd_start
    cmd_check
    cmd_door
}

[ $# -ge 1 ] || { sed -n '2,32p' "$0"; exit 2; }
command=$1
shift
case "$command" in
    fetch) cmd_fetch "$@" ;;
    create) cmd_create ;;
    install) cmd_install "$@" ;;
    lockdown) cmd_lockdown ;;
    start) cmd_start ;;
    door) cmd_door ;;
    check) cmd_check ;;
    provider) cmd_provider "$@" ;;
    status) osh sandbox get "$sandbox"; osh forward list ;;
    shell) osh sandbox connect "$sandbox" ;;
    logs) osh logs "$sandbox" "$@" ;;
    stop) osh forward stop "$door_port" "$sandbox" >/dev/null 2>&1 || true; osh sandbox stop "$sandbox" ;;
    resume) osh sandbox start "$sandbox" ;;
    destroy) cmd_destroy ;;
    up) cmd_up "$@" ;;
    *) die "unknown command '$command' (run without arguments for help)" ;;
esac
