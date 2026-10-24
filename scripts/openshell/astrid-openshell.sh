#!/bin/sh
# Run Astrid inside an NVIDIA OpenShell microVM sandbox instead of your own
# account. The sandbox has its own kernel and sees none of your files. Its
# network access is what you pick: a mode (locked, or read-only dev sources)
# plus services such as Hugging Face or your ChatGPT plan via Codex. Your
# machine's localhost and LAN stay unreachable in every mode. The browser door
# is forwarded to the same 127.0.0.1:17340 that astrid.haus expects.
#
#   scripts/openshell/install-openshell.sh          # once: pinned gateway, no sudo
#   scripts/openshell/astrid-openshell.sh fetch      # download the installer to review
#   scripts/openshell/astrid-openshell.sh up DIR     # create, install, pick services, check, door
#   scripts/openshell/astrid-openshell.sh services   # change mode and services later (menu)
#
# Commands:
#   fetch [DIR]        download https://astrid.haus/install + manifest into DIR, print hashes and pins
#   create             create the sandbox (install-time policy)
#   install DIR        run DIR/install.sh with DIR/install-manifest.json inside the sandbox
#   services [list | add NAME... | remove NAME... | mode locked|dev]
#                      no arguments: interactive menu
#   login NAME | logout NAME   sign a service in or out inside the sandbox (chatgpt, codex)
#   mcp                Astrid as an MCP server on stdio (for Claude Code: claude mcp add astrid -- <this script> mcp)
#   apply              re-apply the saved mode and services
#   lockdown           emergency: mode locked, every service off, providers detached
#   start              start Astrid's Runtime inside the sandbox if it isn't running
#   door               forward 127.0.0.1:17340 on this machine to the sandbox's door
#   check              prove the live policy matches your selection, then probe the network
#   status | shell | logs
#   stop | resume      stop the VM keeping its disk, or start it again (then: start, door)
#   destroy            delete the sandbox and its disk (asks first)
#   up DIR             pick services + create + install DIR + start + check + door
#   rebuild DIR        recreate the sandbox with the saved selection (e.g. to share ComfyUI's output)
#
# Settings (environment):
#   ASTRID_OPENSHELL_HOME     where install-openshell.sh put OpenShell
#   ASTRID_OPENSHELL_GATEWAY  gateway name (default astrid)
#   ASTRID_SANDBOX            sandbox name (default astrid)
#   ASTRID_SANDBOX_IMAGE      sandbox image (default: buildpack-deps noble-scm, pinned by digest)
#   ASTRID_MODE, ASTRID_SERVICES  non-interactive `up`: e.g. ASTRID_MODE=dev ASTRID_SERVICES="chatgpt huggingface comfyui@8188"
#   ASTRID_COMFYUI_PORT, ASTRID_COMFYUI_OUTPUT  skip the ComfyUI questions
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
codex_version=0.160.1   # codex-acp 2.2.2 (Astrid's adapter) depends on @openai/codex ^0.160.1
state="$home/sandboxes/$sandbox.conf"

if [ -x "$home/bin/openshell" ]; then os_cli="$home/bin/openshell"; else os_cli=openshell; fi
if [ -x "$home/bin/openshell-prover" ]; then prover="$home/bin/openshell-prover"; else prover=openshell-prover; fi

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
osh() { "$os_cli" -g "$gateway" "$@"; }
# Never pass our stdin through: `mcp` owns it, and nothing here reads it.
in_sandbox() { osh sandbox exec -n "$sandbox" --no-tty -- sh -c "$1" </dev/null; }
in_sandbox_tty() { osh sandbox exec -n "$sandbox" --tty -- sh -c "$1"; }
command -v python3 >/dev/null 2>&1 || die "python3 is needed on this machine."
compose() { python3 -I "$here/compose_policy.py" "$@"; }
field() { compose field "$1" "$2"; }

# --- saved selection (written by the menu; nobody needs to edit it) ----------
# mode=dev
# services=chatgpt huggingface+cred comfyui@8188
#   +cred: its credential provider is attached; +anon: optional key skipped,
#   don't ask again; @N: the service's parameter (ComfyUI's port)
# comfyui_output=/path/ComfyUI/reports      shared into the sandbox at this path
# comfyui_output_real=/its/real/path        ...from this real (symlink-free) host path
# created_mount=/path/ComfyUI/reports       what the sandbox was created with

load_state() {
    mode=locked
    selected=""
    comfyui_output=""
    comfyui_output_real=""
    created_mount=""
    if [ -f "$state" ]; then
        mode=$(sed -n 's/^mode=\([a-z]*\)$/\1/p' "$state")
        selected=$(sed -n 's/^services=\([a-z0-9@+ ]*\)$/\1/p' "$state")
        comfyui_output=$(sed -n 's/^comfyui_output=\(\/.*\)$/\1/p' "$state")
        comfyui_output_real=$(sed -n 's/^comfyui_output_real=\(\/.*\)$/\1/p' "$state")
        created_mount=$(sed -n 's/^created_mount=\(\/.*\)$/\1/p' "$state")
    fi
    [ -n "$mode" ] || mode=locked
}

save_state() {
    mkdir -p "$(dirname "$state")"
    printf 'mode=%s\nservices=%s\ncomfyui_output=%s\ncomfyui_output_real=%s\ncreated_mount=%s\n' \
        "$mode" "$selected" "$comfyui_output" "$comfyui_output_real" "$created_mount" >"$state"
}

has_service() { case " $selected " in *" $1 "*|*" $1+cred "*|*" $1+anon "*|*" $1@"*) return 0 ;; esac; return 1; }
# The composer only knows +cred; +anon is the menu's own memory.
for_compose() { echo "$*" | sed 's/+anon//g'; }
has_credential() { case " $selected " in *" $1+cred "*) return 0 ;; esac; return 1; }
service_param() { for s in $selected; do case "$s" in "$1@"*) s=${s#*@}; echo "${s%%+*}"; return ;; esac; done; }
drop_service() {
    out=""
    for s in $selected; do
        case "$s" in "$1"|"$1+cred"|"$1+anon"|"$1@"*) ;; *) out="$out $s" ;; esac
    done
    selected=$(echo $out)
}
# The shared ComfyUI output folder is part of every policy for this sandbox.
mount_args() { [ -n "$created_mount" ] && printf -- '--mount %s' "$created_mount"; return 0; }

# --- commands -----------------------------------------------------------------

cmd_fetch() {
    dir=${1:-$home/installers/$(date -u +%Y-%m-%dT%H%M%SZ)}
    mkdir -p "$dir"
    curl --fail --silent --show-error --location https://astrid.haus/install --output "$dir/install.sh"
    curl --fail --silent --show-error --location https://astrid.haus/install-manifest.json --output "$dir/install-manifest.json"
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

set_policy() {   # $1: compose arguments as one string
    file=$(mktemp --suffix=.json)
    # shellcheck disable=SC2086
    compose $(mount_args) $1 >"$file"
    osh policy set "$sandbox" --policy "$file" --wait >/dev/null
    rm -f "$file"
}

cmd_create() {
    if osh sandbox get "$sandbox" >/dev/null 2>&1; then
        say "Sandbox '$sandbox' already exists."
        return
    fi
    load_state
    mounts=""
    created_mount=""
    if has_service comfyui && [ -n "$comfyui_output_real" ]; then
        # One read-only virtiofs share: ComfyUI's output folder, at the path
        # ComfyUI reports (Astrid passes that path as comfy_output_dir).
        mounts=$(python3 -I -c 'import json,sys; print(json.dumps({"vm": {"mounts": [{"type": "bind", "source": sys.argv[1], "target": sys.argv[2], "read_only": True}]}}))' \
            "$comfyui_output_real" "$comfyui_output")
        created_mount=$comfyui_output
        say "Sharing ComfyUI's output folder read-only: $comfyui_output_real -> $comfyui_output"
    fi
    file=$(mktemp --suffix=.json)
    # shellcheck disable=SC2046
    compose $(mount_args) install >"$file"
    # --no-auto-providers: never copy credentials from this shell into the
    # sandbox. `sleep infinity` keeps the VM up; Runtime runs beside it.
    if [ -n "$mounts" ]; then
        osh sandbox create --name "$sandbox" --from "$image" --policy "$file" \
            --no-auto-providers --no-tty --label app=astrid \
            --driver-config-json "$mounts" --detach -- sleep infinity
    else
        osh sandbox create --name "$sandbox" --from "$image" --policy "$file" \
            --no-auto-providers --no-tty --label app=astrid --detach -- sleep infinity
    fi
    rm -f "$file"
    save_state
}

# Copy the installer (and any local Git sources its manifest names) into the
# sandbox, rewriting local `repo` paths to their copies inside the sandbox.
stage_installer() {
    src=$1
    [ -f "$src/install.sh" ] && [ -f "$src/install-manifest.json" ] \
        || die "$src must contain install.sh and install-manifest.json (see: fetch)."
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
    # A bare clone is enough for the installer's `git archive`.
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
    set_policy install
    stage_installer "$1"
    say "Running Astrid's installer inside '$sandbox' (several minutes the first time)."
    in_sandbox "cd '$guest_home/$stage_name' && sha256sum install.sh install-manifest.json && ASTRID_INSTALL_MANIFEST_URL='file://$guest_home/$stage_name/install-manifest.json' sh ./install.sh"
}

# --- service setup ------------------------------------------------------------

setup_codex() {
    if in_sandbox "test -x '$guest_home/.local/bin/codex' && grep -qs '^sandbox_mode' '$guest_home/.codex/config.toml' && '$guest_home/.local/bin/codex' --version 2>/dev/null | grep -q '$codex_version'"; then
        return
    fi
    say "Installing Codex CLI $codex_version inside the sandbox (install-time policy for a moment)."
    set_policy install
    in_sandbox "set -e
r=\$HOME/.local/share/astrid; v=\$(cat \"\$r/current\")
node=\$(ls \"\$r\"/versions/\"\$v\"/tools/node/*/bin/node)
npm=\$(dirname \"\$node\")/../lib/node_modules/npm/bin/npm-cli.js
\"\$node\" \"\$npm\" install --global --prefix \"\$HOME/.local\" --no-fund --no-audit --loglevel=error @openai/codex@$codex_version
# npm's codex link needs \`node\` on PATH; run it with Astrid's Node instead.
rm -f \"\$HOME/.local/bin/codex\"
cat >\"\$HOME/.local/bin/codex\" <<'WRAP'
#!/bin/sh
r=\$HOME/.local/share/astrid; v=\$(cat \"\$r/current\")
for node in \"\$r\"/versions/\"\$v\"/tools/node/*/bin/node; do
    exec \"\$node\" \"\$HOME/.local/lib/node_modules/@openai/codex/bin/codex.js\" \"\$@\"
done
echo 'codex: Astrid Node not found' >&2; exit 1
WRAP
chmod 755 \"\$HOME/.local/bin/codex\"
# Astrid finds agents on the login-shell PATH (bash -lic).
grep -qs 'astrid-openshell' \"\$HOME/.profile\" || printf '%s\n' 'PATH=\"\$HOME/.local/bin:\$PATH\"  # astrid-openshell' >>\"\$HOME/.profile\"
# Codex's own sandbox needs user namespaces (bubblewrap), which OpenShell
# denies, so every shell command would fail. The OpenShell VM is the
# boundary here; let Codex run commands directly inside it.
mkdir -p \"\$HOME/.codex\"
grep -qs '^sandbox_mode' \"\$HOME/.codex/config.toml\" || printf '%s\n' '# astrid-openshell: the OpenShell sandbox is the boundary (bubblewrap cannot run inside it)' 'sandbox_mode = \"danger-full-access\"' >>\"\$HOME/.codex/config.toml\"
\"\$HOME/.local/bin/codex\" --version"
}

# Read a service's key from its environment variable, or ask for it (hidden).
read_key() {
    env_var=$(field "$1" credential_env)
    eval "value=\${$env_var:-}"
    if [ -z "$value" ] && [ -t 0 ]; then
        printf '%s: ' "$(field "$1" credential_prompt)"
        stty -echo 2>/dev/null || true
        read -r value || value=""
        stty echo 2>/dev/null || true
        printf '\n'
    fi
    [ -n "$value" ]
}

# Create or update the OpenShell provider for a credential service (key from
# read_key) and attach it.
attach_credential() {
    name=$1
    profile=$(mktemp --suffix=.yaml)
    compose profile "$name" >"$profile"
    osh profile import -f "$profile" >/dev/null 2>&1 || osh profile update -f "$profile" "astrid-$name" >/dev/null
    rm -f "$profile"
    # The key reaches openshell through its environment, never its arguments.
    if osh provider get "astrid-$name" >/dev/null 2>&1; then
        env "$env_var=$value" "$os_cli" -g "$gateway" provider update "astrid-$name" --credential "$env_var" >/dev/null
    else
        env "$env_var=$value" "$os_cli" -g "$gateway" provider create --name "astrid-$name" --type "astrid-$name" --credential "$env_var" >/dev/null
    fi
    value=""
    osh sandbox provider attach "$sandbox" "astrid-$name" --wait >/dev/null
    say "  $(field "$name" title): key stored in OpenShell; the sandbox sees a placeholder."
}

detach_credential() {
    osh sandbox provider detach "$sandbox" "astrid-$1" --wait >/dev/null 2>&1 || true
}

detect_comfy_output() {
    python3 -I - "$1" <<'EOF'
import json, sys, urllib.request
from pathlib import Path
url = f"http://127.0.0.1:{sys.argv[1]}"
get = lambda path: json.load(urllib.request.urlopen(url + path, timeout=5))
argv = [str(a) for a in get("/system_stats").get("system", {}).get("argv", [])]
for i, arg in enumerate(argv):
    if arg == "--output-directory" and i + 1 < len(argv):
        print(argv[i + 1]); raise SystemExit
    if arg.startswith("--output-directory="):
        print(arg.partition("=")[2]); raise SystemExit
nodes = get("/internal/folder_paths").get("custom_nodes")
nodes = nodes if isinstance(nodes, str) else next(p for p in nodes if isinstance(p, str) and p)
print(Path(nodes).parent / "output")
EOF
}

# Ask for ComfyUI's port, find its output folder the way Astrid's own scan does
# (--output-directory, else <base>/output), and resolve it to a real path.
configure_comfyui() {
    port=${ASTRID_COMFYUI_PORT:-}
    if [ -z "$port" ] && [ -t 0 ]; then
        printf 'ComfyUI port on this machine [8188]: '
        read -r port || port=""
    fi
    port=${port:-8188}
    case "$port" in *[!0-9]*|'') die "not a port: $port" ;; esac
    out=${ASTRID_COMFYUI_OUTPUT:-}
    if [ -z "$out" ]; then
        out=$(detect_comfy_output "$port" 2>/dev/null) || out=""
        [ -n "$out" ] && say "  ComfyUI on port $port reports its output folder as $out"
    fi
    if [ -t 0 ]; then
        printf 'ComfyUI output folder [%s]: ' "${out:-?}"
        read -r answer || answer=""
        [ -n "$answer" ] && out=$answer
    fi
    [ -n "$out" ] || die "could not find ComfyUI's output folder (is it running on port $port?); set ASTRID_COMFYUI_OUTPUT"
    case "$out" in /*) ;; *) die "the output folder must be an absolute path" ;; esac
    real=$(python3 -I -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$out")
    [ -d "$real" ] || die "$out is not a folder"
    [ "$real" != "$out" ] && say "  $out is (or passes through) a symlink; sharing its real path $real"
    comfyui_output=$out
    comfyui_output_real=$real
    drop_service comfyui
    selected=$(echo "$selected comfyui@$port")
}

# Inside the sandbox: relay 127.0.0.1:PORT to the host's ComfyUI (Astrid only
# accepts loopback URLs). Restarted by `start` after the VM resumes.
setup_comfyui() {
    staging=$(mktemp -d)
    mkdir -p "$staging/astrid-openshell"
    cp "$here/sandbox/comfyui-relay.py" "$staging/astrid-openshell/"
    in_sandbox "rm -f '$guest_home/astrid-openshell/comfyui-relay.py'"
    osh sandbox upload "$sandbox" "$staging/astrid-openshell" "$guest_home" >/dev/null
    rm -rf "$staging"
    # Restart so an updated relay script takes effect.
    in_sandbox "d=\$HOME/astrid-openshell; [ -f \$d/relay.pid ] && kill \$(cat \$d/relay.pid) 2>/dev/null; rm -f \$d/relay.pid"
    start_relay
}

start_relay() {
    port=$(service_param comfyui)
    [ -n "$port" ] || return 0
    in_sandbox "d=\$HOME/astrid-openshell
if [ -f \$d/relay.pid ] && kill -0 \$(cat \$d/relay.pid) 2>/dev/null; then
    [ \"\$(cat \$d/relay.port 2>/dev/null)\" = $port ] && exit 0
    kill \$(cat \$d/relay.pid)
fi
py=\$(ls \$HOME/.local/share/astrid/bootstrap/python/*/bin/python3 | head -1)
setsid nohup \"\$py\" -I \$d/comfyui-relay.py $port >\$d/relay.log 2>&1 </dev/null &
echo \$! >\$d/relay.pid; echo $port >\$d/relay.port"
    say "  ComfyUI relay: 127.0.0.1:$port in the sandbox -> this machine's port $port."
}

# Make the sandbox match the saved mode and services.
cmd_apply() {
    load_state
    # Detach providers that are no longer wanted before narrowing the policy.
    attached=$(osh sandbox provider list "$sandbox" 2>/dev/null | sed -n 's/.*astrid-\([a-z]*\).*/\1/p' | sort -u)
    for name in $attached; do
        has_credential "$name" || detach_credential "$name"
    done
    for name in $selected; do
        name=${name%+*}; name=${name%@*}
        [ "$(field "$name" setup)" = codex ] && setup_codex
    done
    # Credential services: try to attach; optional ones fall back to no key.
    final=""
    for item in $selected; do
        name=${item%+*}; name=${name%@*}
        if [ -n "$(field "$name" credential_env)" ] && [ "$item" != "$name" ]; then
            final="$final $item"
        elif [ -n "$(field "$name" credential_env)" ]; then
            if read_key "$name"; then
                # Drop any anonymous rule for the provider's host first, so the
                # two rules never coexist with different credential settings.
                set_policy "run $mode $(for_compose $final) $name+cred"
                attach_credential "$name"
                final="$final $name+cred"
            elif [ -n "$(field "$name" credential_optional)" ]; then
                final="$final $name+anon"
                say "  $(field "$name" title): no key, public access only (switch it off and on to add one)."
            else
                say "  $(field "$name" title): no key given, skipped."
            fi
        else
            final="$final $item"
        fi
    done
    selected=$(echo $final)
    save_state
    # shellcheck disable=SC2086
    set_policy "run $mode $(for_compose $selected)"
    # Keys saved in OpenShell earlier (e.g. before a rebuild) are re-attached.
    for item in $selected; do
        case "$item" in *+cred) ;; *) continue ;; esac
        name=${item%+cred}
        echo "$attached" | grep -qx "$name" || osh sandbox provider attach "$sandbox" "astrid-$name" --wait >/dev/null
    done
    say "Applied: mode $mode; services: ${selected:-none}."
    if has_service comfyui; then
        if [ "$created_mount" = "$comfyui_output" ] && [ -n "$created_mount" ]; then
            setup_comfyui
        else
            say "  ComfyUI: the sandbox was created without its output folder; run  astrid-openshell.sh rebuild DIR  to share it."
        fi
    fi
    for name in $selected; do
        name=${name%+*}; name=${name%@*}
        login=$(field "$name" login)
        [ -n "$login" ] && say "  $(field "$name" title): sign in with  astrid-openshell.sh login $name"
    done
    return 0
}

cmd_services() {
    load_state
    case "${1:-menu}" in
        list)
            say "Mode: $mode"
            compose menu | while IFS="$(printf '\t')" read -r kind id title summary; do
                [ "$kind" = service ] || continue
                if has_service "$id"; then mark=x; else mark=' '; fi
                printf '  [%s] %-12s %s: %s\n' "$mark" "$id" "$title" "$summary"
            done
            return ;;
        mode)
            [ $# -ge 2 ] || die "usage: services mode locked|dev"
            mode=$2; save_state; cmd_apply; return ;;
        add)
            shift
            for name in "$@"; do
                [ -n "$(field "$name" title 2>/dev/null)" ] || die "unknown service '$name' (see: services list)"
                if [ "$name" = comfyui ]; then configure_comfyui
                else has_service "$name" || selected="$selected $name"; fi
            done
            selected=$(echo $selected); save_state; cmd_apply; return ;;
        remove)
            shift
            for name in "$@"; do drop_service "$name"; done
            save_state; cmd_apply; return ;;
        menu) ;;
        *) die "usage: services [list | add NAME... | remove NAME... | mode locked|dev]" ;;
    esac
    [ -t 0 ] || die "the menu needs a terminal; use: services add|remove|mode"
    while :; do
        say ""
        say "Astrid sandbox '$sandbox': network and services"
        say "Your machine's localhost and LAN are blocked in every mode."
        i=0
        compose menu >"${TMPDIR:-/tmp}/astrid-menu.$$"
        while IFS="$(printf '\t')" read -r kind id title summary; do
            if [ "$kind" = mode ]; then
                if [ "$id" = "$mode" ]; then mark='*'; else mark=' '; fi
                printf '  mode %s %-8s %s: %s\n' "$mark" "$id" "$title" "$summary"
            else
                i=$((i + 1))
                if has_service "$id"; then mark=x; else mark=' '; fi
                printf '  %2d) [%s] %s: %s\n' "$i" "$mark" "$title" "$summary"
            fi
        done <"${TMPDIR:-/tmp}/astrid-menu.$$"
        printf 'Number to switch a service on/off, m to change mode, a to apply, q to quit: '
        read -r choice || choice=q
        case "$choice" in
            q) rm -f "${TMPDIR:-/tmp}/astrid-menu.$$"; say "No changes applied."; return ;;
            a) rm -f "${TMPDIR:-/tmp}/astrid-menu.$$"; save_state
               [ -n "${menu_save_only:-}" ] || cmd_apply; return ;;
            m) if [ "$mode" = locked ]; then mode=dev; else mode=locked; fi ;;
            *[!0-9]*|'') say "?" ;;
            *)
                id=$(awk -F '\t' -v n="$choice" '$1=="service"{c++; if (c==n) print $2}' "${TMPDIR:-/tmp}/astrid-menu.$$")
                if [ -z "$id" ]; then say "?"
                elif has_service "$id"; then drop_service "$id"
                else
                    note=$(field "$id" note)
                    [ -n "$note" ] && say "  note: $note"
                    if [ "$id" = comfyui ]; then configure_comfyui
                    else selected=$(echo "$selected $id"); fi
                fi ;;
        esac
    done
}

cmd_login() {
    [ $# -ge 1 ] || die "usage: login NAME"
    command=$(field "$1" login)
    [ -n "$command" ] || die "'$1' has no sign-in step"
    load_state
    has_service "$1" || die "turn the service on first: astrid-openshell.sh services add $1"
    note=$(field "$1" note); [ -n "$note" ] && say "$note"
    note=$(field "$1" login_note); [ -n "$note" ] && say "$note"
    # OAuth callbacks to localhost:<port> land on this machine; forward them in
    # for the length of the sign-in.
    port=$(field "$1" login_forward)
    if [ -n "$port" ] && ! osh forward list 2>/dev/null | grep -w "$sandbox" | grep -qw "$port"; then
        osh forward start "127.0.0.1:$port" "$sandbox" --background </dev/null >/dev/null 2>&1
        trap 'osh forward stop "$port" "$sandbox" >/dev/null 2>&1 || true' EXIT
    fi
    in_sandbox_tty "PATH=\$HOME/.local/bin:\$PATH; $command"
}

cmd_logout() {
    [ $# -ge 1 ] || die "usage: logout NAME"
    command=$(field "$1" logout)
    [ -n "$command" ] || die "'$1' has no sign-out step"
    note=$(field "$1" logout_note); [ -n "$note" ] && say "$note"
    in_sandbox_tty "PATH=\$HOME/.local/bin:\$PATH; $command"
}

# MCP server (python -m astrid.mcp_server) inside the sandbox, on this
# command's stdin/stdout, for an MCP client on this machine:
#   claude mcp add astrid -- /path/to/astrid-openshell.sh mcp
# Until Astrid ships the module, a copy from this checkout is uploaded when the
# sandbox's copy is missing or different. Nothing but MCP goes to stdout.
cmd_mcp() {
    src="$here/../../astrid/mcp_server.py"
    if in_sandbox "r=\$HOME/.local/share/astrid; \$r/versions/\$(cat \$r/current)/py/bin/python -I -c 'import astrid.mcp_server'" >/dev/null 2>&1; then
        launch='exec "$py" -I -m astrid.mcp_server'
    elif [ -f "$src" ]; then
        want=$(sha256sum "$src" | cut -d' ' -f1)
        have=$(in_sandbox "sha256sum \$HOME/astrid-mcp/mcp_server.py 2>/dev/null | cut -d' ' -f1" 2>/dev/null || true)
        if [ "$want" != "$have" ]; then
            staging=$(mktemp -d)
            mkdir -p "$staging/astrid-mcp"
            cp "$src" "$staging/astrid-mcp/mcp_server.py"
            in_sandbox "rm -rf \$HOME/astrid-mcp" >&2
            osh sandbox upload "$sandbox" "$staging/astrid-mcp" "$guest_home" >&2 2>&1
            rm -rf "$staging"
        fi
        launch='exec "$py" -I "$HOME/astrid-mcp/mcp_server.py"'
    else
        die "this Astrid has no MCP server (astrid.mcp_server) and no copy was found at $src"
    fi
    exec "$os_cli" -g "$gateway" sandbox exec -n "$sandbox" --no-tty -- sh -c \
        "r=\$HOME/.local/share/astrid; py=\$r/versions/\$(cat \$r/current)/py/bin/python; $launch"
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
    # re-bootstraps from the network. Start Runtime the way the installer's
    # no-systemd fallback does. Stopping a running Runtime is not possible from
    # here: OpenShell's seccomp filter denies pidfds and Runtime refuses to
    # signal a bare PID, so restart = stop + resume the VM, then start.
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

cmd_start_all() {
    cmd_start
    load_state
    start_relay
}

cmd_door() {
    if osh forward list 2>/dev/null | grep -w "$sandbox" | grep -q "$door_port"; then
        say "Door already forwarded."
    else
        # The background forward must not hold our stdout open (callers may pipe it).
        osh forward start "127.0.0.1:$door_port" "$sandbox" --background </dev/null >/dev/null 2>&1
    fi
    code=$(curl --silent --output /dev/null --write-out '%{http_code}' -H 'Origin: https://astrid.haus' "http://127.0.0.1:$door_port/" || true)
    case "$code" in
        403|404|200) say "Door: http://127.0.0.1:$door_port (answering, HTTP $code). Open https://astrid.haus in a separate browser profile." ;;
        *) say "Door forwarded but not answering yet (HTTP $code). Try: astrid-openshell.sh start" ;;
    esac
}

cmd_check() {
    load_state
    tmp=$(mktemp -d)
    osh policy get "$sandbox" --full -o json | python3 -I -c '
import json, sys
policy = json.load(sys.stdin)["policy"]
policy["version"] = 1
json.dump(policy, sys.stdout)' >"$tmp/live.json"
    # shellcheck disable=SC2086
    compose $(mount_args) boundary "$mode" $(for_compose $selected) >"$tmp/boundary.json"
    # The prover models neither allow_encoded_slash (npm scoped packages,
    # ComfyUI's saved workflows) nor WebSocket endpoints (ComfyUI's /ws). Every
    # such endpoint in the live policy must appear exactly in the composed one;
    # then both are removed from both sides and the rest is proved.
    python3 -I - "$tmp/live.json" "$tmp/boundary.json" <<'EOF' || die "the live policy has endpoints the selection does not"
import json, sys
def split(path):
    policy = json.load(open(path))
    extra = set()
    for name, rule in policy.get("network_policies", {}).items():
        bins = json.dumps(sorted(b["path"] for b in rule.get("binaries", [])))
        kept = []
        for e in rule.get("endpoints", []):
            if e.get("protocol") == "websocket":
                extra.add(json.dumps(e, sort_keys=True) + bins)
                continue
            if e.pop("allow_encoded_slash", False):
                extra.add(e["host"] + ":" + str(e["port"]) + " encoded-slash " + bins)
            kept.append(e)
        rule["endpoints"] = kept
    json.dump(policy, open(path, "w"))
    return extra
live, intended = split(sys.argv[1]), split(sys.argv[2])
for item in sorted(live - intended):
    print("  unexpected:", item[:160])
raise SystemExit(1 if live - intended else 0)
EOF
    result=$("$prover" check --boundary "$tmp/boundary.json" "$tmp/live.json" || true)
    rm -rf "$tmp"
    printf '%s\n' "$result" | head -3
    # The prover exits 0 for every verdict; only this line means "proved".
    printf '%s\n' "$result" | grep -q '^result: within_boundary$' \
        || die "the live policy allows more than mode '$mode' + services '${selected:-none}'"

    allowed=""
    if [ "$mode" = dev ]; then allowed="https://pypi.org/simple/"; fi
    comfy_port=$(service_param comfyui)
    host_ports=""
    for p in 8188 8000 8189; do [ "$p" = "$comfy_port" ] || host_ports="$host_ports http://host.openshell.internal:$p"; done
    say "Probing from inside the sandbox (mode $mode):"
    in_sandbox "
fail=0
probe() { curl --silent --max-time 6 --output /dev/null --write-out '%{http_code}' \"\$@\" 2>/dev/null; }
for url in https://example.com https://astrid.haus $host_ports \
           https://host.openshell.internal:17670 http://169.254.169.254/ http://10.0.0.1/ http://192.168.1.1/; do
    code=\$(probe \"\$url\") || true
    case \"\$code\" in 000|403) echo \"  blocked    \$url\" ;; *) echo \"  REACHABLE  \$url (HTTP \$code)\"; fail=1 ;; esac
done
# Uploads must be blocked even where downloads are allowed (403 = OpenShell's proxy).
code=\$(probe -X POST -d x https://api.github.com/gists) || true
case \"\$code\" in 000|403) echo '  blocked    POST https://api.github.com/gists' ;; *) echo \"  REACHABLE  POST https://api.github.com/gists (HTTP \$code)\"; fail=1 ;; esac
for url in $allowed; do
    code=\$(probe \"\$url\") || true
    case \"\$code\" in 2??|3??) echo \"  allowed    \$url (HTTP \$code)\" ;; *) echo \"  FAILED     \$url should be reachable in this mode (HTTP \$code)\"; fail=1 ;; esac
done
for path in /home /root /var/run/docker.sock /run/docker.sock $([ -n "$created_mount" ] || echo /mnt); do
    [ -e \"\$path\" ] && [ -n \"\$(ls -A \"\$path\" 2>/dev/null)\" ] && { echo \"  VISIBLE    \$path\"; fail=1; } || echo \"  absent     \$path\"
done
if [ -n '$comfy_port' ]; then
    # ComfyUI through the relay: the core API answers, Manager and settings writes do not.
    py=\$(ls \$HOME/.local/share/astrid/bootstrap/python/*/bin/python3 | head -1)
    \"\$py\" -I - '$comfy_port' <<'PY' || fail=1
import sys, urllib.request, urllib.error
base = f\"http://127.0.0.1:{sys.argv[1]}\"
def code(method, path, data=None):
    try:
        return urllib.request.urlopen(urllib.request.Request(base + path, data=data, method=method), timeout=8).status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return 0
bad = 0
for method, path, want in [(\"GET\", \"/system_stats\", \"ok\"), (\"GET\", \"/internal/folder_paths\", \"ok\"),
                           (\"GET\", \"/api/manager/version\", \"deny\"), (\"GET\", \"/manager/version\", \"deny\"),
                           (\"POST\", \"/userdata/astrid-probe.json\", \"deny\"), (\"POST\", \"/settings/astrid.probe\", \"deny\")]:
    c = code(method, path, b\"{}\" if method == \"POST\" else None)
    ok = (200 <= c < 300) if want == \"ok\" else c in (0, 403)
    print(f\"  {'allowed' if want == 'ok' else 'blocked'}    {method} ComfyUI {path} (HTTP {c})\" if ok else f\"  WRONG      {method} ComfyUI {path} (HTTP {c})\")
    bad |= not ok
raise SystemExit(bad)
PY
fi
if [ -n '$created_mount' ]; then
    if ls '$created_mount' >/dev/null 2>&1; then echo '  readable   $created_mount (ComfyUI output, shared read-only)'; else echo '  UNREADABLE $created_mount'; fail=1; fi
    if touch '$created_mount/.astrid-write-probe' 2>/dev/null; then echo '  WRITABLE   $created_mount'; rm -f '$created_mount/.astrid-write-probe'; fail=1; else echo '  read-only  $created_mount'; fi
fi
exit \$fail" || die "a network or host-path probe did not behave as expected"
    say "Check passed."
}

cmd_destroy() {
    printf "Delete sandbox '%s' and everything in it? [y/N] " "$sandbox"
    read -r answer
    [ "$answer" = y ] || [ "$answer" = Y ] || { say "Kept."; return; }
    osh forward stop "$door_port" "$sandbox" >/dev/null 2>&1 || true
    for name in $(osh sandbox provider list "$sandbox" 2>/dev/null | sed -n 's/.*astrid-\([a-z]*\).*/\1/p' | sort -u); do
        detach_credential "$name"
    done
    osh sandbox delete "$sandbox"
    rm -f "$state"
}

cmd_up() {
    [ $# -ge 1 ] || die "usage: up DIR (from: fetch)"
    load_state
    # Choose services before creating the sandbox: ComfyUI's shared folder is
    # fixed at creation. A saved selection (e.g. from `rebuild`) is reused.
    if [ -n "${ASTRID_MODE:-}${ASTRID_SERVICES:-}" ]; then
        mode=${ASTRID_MODE:-locked}
        selected=""
        for name in ${ASTRID_SERVICES:-}; do
            if [ "${name%%@*}" = comfyui ]; then
                case "$name" in *@*) ASTRID_COMFYUI_PORT=${name#*@} ;; esac
                configure_comfyui
            else selected="$selected $name"; fi
        done
        selected=$(echo $selected)
        save_state
    elif [ ! -f "$state" ] && [ -t 0 ]; then
        say "Pick what the sandbox may reach (you can change this later with: astrid-openshell.sh services)."
        menu_save_only=1 cmd_services menu
        [ -f "$state" ] || save_state
    fi
    cmd_create
    cmd_install "$1"
    cmd_apply
    cmd_start_all
    cmd_check
    cmd_door
}

# Recreate the sandbox with the saved selection (needed to share ComfyUI's
# output folder). Signed-in sessions and anything else inside are lost.
cmd_rebuild() {
    [ $# -ge 1 ] || die "usage: rebuild DIR (the installer folder, see: fetch)"
    load_state
    printf "Recreate sandbox '%s' (everything inside it is deleted, including sign-ins)? [y/N] " "$sandbox"
    if [ "${ASTRID_YES:-}" = 1 ]; then answer=y; echo y; else read -r answer || answer=""; fi
    [ "$answer" = y ] || [ "$answer" = Y ] || { say "Kept."; return; }
    osh forward stop "$door_port" "$sandbox" >/dev/null 2>&1 || true
    for name in $(osh sandbox provider list "$sandbox" 2>/dev/null | sed -n 's/.*astrid-\([a-z]*\).*/\1/p' | sort -u); do
        detach_credential "$name"
    done
    osh sandbox delete "$sandbox" >/dev/null 2>&1 || true
    # The keys stay in OpenShell; apply re-attaches them to the new sandbox.
    cmd_up "$1"
}

[ $# -ge 1 ] || { sed -n '2,40p' "$0"; exit 2; }
command=$1
shift
case "$command" in
    fetch) cmd_fetch "$@" ;;
    create) cmd_create ;;
    install) cmd_install "$@" ;;
    services) cmd_services "$@" ;;
    apply) cmd_apply ;;
    login) cmd_login "$@" ;;
    logout) cmd_logout "$@" ;;
    mcp) cmd_mcp ;;
    lockdown) load_state; mode=locked; selected=""; save_state; cmd_apply ;;
    start) cmd_start_all ;;
    rebuild) cmd_rebuild "$@" ;;
    door) cmd_door ;;
    check) cmd_check ;;
    status) load_state; say "Mode: $mode; services: ${selected:-none}"; osh sandbox get "$sandbox"; osh forward list ;;
    shell) osh sandbox connect "$sandbox" ;;
    logs) osh logs "$sandbox" "$@" ;;
    stop) osh forward stop "$door_port" "$sandbox" >/dev/null 2>&1 || true; osh sandbox stop "$sandbox" ;;
    resume) osh sandbox start "$sandbox" ;;
    destroy) cmd_destroy ;;
    up) cmd_up "$@" ;;
    *) die "unknown command '$command' (run without arguments for help)" ;;
esac
