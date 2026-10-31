#!/bin/sh
# Astrid's bootstrap. Install policy and recovery live in astrid.installer.
set -eu
umask 077
UV_VERSION=0.11.32
PYTHON_VERSION=3.11.13
case "$(uname -s)" in
    Darwin) data_home="$HOME/.local/share"; log="$HOME/Library/Logs/Astrid Runtime/install.log" ;;
    Linux) data_home=${XDG_DATA_HOME:-$HOME/.local/share}; log="${XDG_STATE_HOME:-$HOME/.local/state}/astrid-runtime/logs/install.log" ;;
    *) echo 'Use https://astrid.haus/install.ps1 on Windows.' >&2; exit 1 ;; esac
bootstrap="$data_home/astrid/bootstrap"
mkdir -p "$bootstrap" "$(dirname "$log")"
chmod 700 "$(dirname "$log")"
tmp=$(mktemp -d "${TMPDIR:-/tmp}/astrid-install.XXXXXXXX")
trap 'rm -rf "$tmp"' EXIT
trap 'exit 129' HUP
trap 'exit 143' TERM
# Let the foreground installer finish its rollback after the first Ctrl+C.
trap ':' INT
fetch() {
    if command -v curl >/dev/null 2>&1; then
        curl --fail --silent --show-error --location "$1" --output "$2"
    else
        wget -q "$1" -O "$2"
    fi
}
fail() { echo "Astrid couldn't finish. Your work is kept." >&2; echo "Details: $log" >&2; exit 1; }
echo 'Getting Astrid ready. The first time takes a few minutes.'
manifest="$tmp/manifest.json"
manifest_next=0
manifest_provided=0
for arg in "$@"; do
    if [ "$manifest_next" -eq 1 ]; then
        manifest=$arg
        manifest_provided=1
        manifest_next=0
        continue
    fi
    case "$arg" in
        --manifest) manifest_next=1; manifest_provided=1 ;;
        --manifest=*) manifest=${arg#--manifest=}; manifest_provided=1 ;;
    esac
done
if [ "$manifest_next" -eq 1 ] || [ -z "$manifest" ]; then
    echo 'A path is required after --manifest.' >&2
    exit 2
fi
if [ "$manifest_provided" -ne 1 ]; then
    manifest_url=${ASTRID_INSTALL_MANIFEST_URL:-https://astrid.haus/install-manifest.json}
    fetch "$manifest_url" "$manifest" 2>>"$log" || fail
fi
uv="$bootstrap/uv"
uv_matches=0
if [ -x "$uv" ]; then
    uv_found=$("$uv" --version 2>>"$log") || uv_found=
    case "$uv_found" in
        "uv $UV_VERSION"|"uv $UV_VERSION "*) uv_matches=1 ;;
    esac
fi
if [ "$uv_matches" -ne 1 ]; then
    fetch "https://astral.sh/uv/$UV_VERSION/install.sh" "$tmp/uv.sh" 2>>"$log" || fail
    UV_UNMANAGED_INSTALL="$bootstrap" UV_NO_MODIFY_PATH=1 sh "$tmp/uv.sh" >>"$log" 2>&1 || fail
fi
export UV_PYTHON_INSTALL_DIR="$bootstrap/python"
export ASTRID_INSTALL_UV="$uv"
"$uv" python install "$PYTHON_VERSION" --no-bin >>"$log" 2>&1 || fail
# With curl | sh, uv would inherit the script pipe and forward a second
# SIGINT after the terminal already delivered one. Keep interactive stdin.
if (: </dev/tty) 2>/dev/null; then exec 3</dev/tty; else exec 3</dev/null; fi
# Keep stdout live and retain its status independently of the progress reader.
mkfifo "$tmp/progress"
tee -i "$tmp/core.out" <"$tmp/progress" &
progress_pid=$!
# This only resolves manifest pins and hands off; the package owns every step.
if "$uv" run --no-project --python "$PYTHON_VERSION" python -u -I -c '
import json, os, re, subprocess, sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname
manifest, *flags = sys.argv[1:]
with open(manifest, encoding="utf-8") as stream: pins = json.load(stream)
if pins["tools"]["uv"]["version"] != "0.11.32" or pins["tools"]["python"]["version"] != "3.11.13":
    raise SystemExit("The bootstrap and manifest tool pins disagree.")
def git_requirement(name):
    pin = pins[name]
    sha = pin["sha"]
    if not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{40}", sha):
        raise SystemExit("Invalid source pin")
    if "repo" not in pin:
        return "https://github.com/banodoco/astrid-" + name + "/archive/" + sha + ".tar.gz"
    repo = pin["repo"]
    if not isinstance(repo, str) or not repo:
        raise SystemExit("Source repo must be an absolute local path or file:// URL")
    if repo.startswith("file://"):
        parsed = urlsplit(repo)
        if parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
            raise SystemExit("Invalid local source URL")
        repo = url2pathname(parsed.path)
    path = Path(repo)
    if not path.is_absolute(): raise SystemExit("Source repo must be absolute")
    # uv reads the committed Git tree, never the mutable checkout.
    return "git+" + path.as_uri() + "@" + sha
uv = os.environ["ASTRID_INSTALL_UV"]
agent_requirement = git_requirement("agent")
venv = Path(manifest).with_name("bootstrap-venv")
subprocess.run([uv, "venv", str(venv), "--python", pins["tools"]["python"]["version"]], check=True)
python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
runtime = git_requirement("runtime")
client = runtime + "#subdirectory=packages/python"
subprocess.run([uv, "pip", "install", "--python", str(python), "--no-deps", runtime, client, agent_requirement], check=True)
has_manifest = any(flag == "--manifest" or flag.startswith("--manifest=") for flag in flags)
args = [uv, "run", "--no-project", "--python", str(python), "python", "-u", "-I", "-m", "astrid", "install"]
if not has_manifest: args.extend(["--manifest", manifest])
args.extend(flags)
os.execv(args[0], args)
' "$manifest" "$@" <&3 >"$tmp/progress" 2>>"$log"; then status=0; else status=$?; fi
wait "$progress_pid"
if [ "$status" -ne 0 ]; then
    if ! grep -Fq "Astrid couldn't finish. Your work is kept." "$tmp/core.out"; then
        echo "Astrid couldn't finish. Your work is kept." >&2
        echo "Details: $log" >&2
    fi
    exit "$status"
fi
