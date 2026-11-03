#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'USAGE'
Usage: pinned-env.sh (--locked-env ENV_DIR | --python PATH --agent-pin SHA \
  --runtime-pin SHA --vibecomfy-pin SHA --sdk-pin SHA) --agent-root DIR \
  --runtime-root DIR --vibecomfy-root DIR --sdk-root DIR --lifecycle-root DIR \
  [--lifecycle-pin SHA --runpod-sdk-root DIR --runpod-sdk-pin SHA] \
  [--version NAME=VERSION ... --distribution NAME=VERSION ...] \
  --node PYTEST_NODE [--node PYTEST_NODE ...]

The locked profile requires --runpod-sdk-root and fixes pins, versions and distributions; do not pass overrides.
For the generic profile, VERSION is an exact installed distribution version, or ABSENT to assert that
the distribution has no installed metadata. All root and pin values are
required. Node validation uses pytest --collect-only and never runs tests.
USAGE
}

declare -A value=()
versions=()
distributions=()
nodes=()
while (($#)); do
  case "$1" in
    --locked-env|--lifecycle-pin|--runpod-sdk-root|--runpod-sdk-pin|--python|--agent-root|--agent-pin|--runtime-root|--runtime-pin|--vibecomfy-root|--vibecomfy-pin|--sdk-root|--sdk-pin|--lifecycle-root|--version|--distribution|--node)
      (($# >= 2)) || { usage >&2; exit 2; }
      key=${1#--}; arg=$2; shift 2
      case "$key" in
        version) versions+=("$arg");;
        distribution) distributions+=("$arg");;
        node) nodes+=("$arg");;
        *) value["$key"]=$arg;;
      esac
      ;;
    -h|--help) usage; exit 0;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2;;
  esac
done

# The locked profile fixes pins and versions; callers supply only roots and nodes.
locked=0
if [[ -n ${value[locked-env]:-} ]]; then
  locked=1
  ((${#versions[@]} == 0 && ${#distributions[@]} == 0)) || { echo 'locked profile does not accept version overrides' >&2; exit 2; }
  for key in python agent-pin runtime-pin vibecomfy-pin sdk-pin lifecycle-pin runpod-sdk-pin; do
    [[ -z ${value[$key]:-} ]] || { echo "locked profile does not accept --$key" >&2; exit 2; }
  done
  value[python]="${value[locked-env]}/bin/python"
  value[agent-pin]=ceed0cd46426c9619d055881b5736183c381ab42
  value[runtime-pin]=1a275bb339ae897e52a5be5e1da99e846e402037
  value[sdk-pin]=1a275bb339ae897e52a5be5e1da99e846e402037
  value[vibecomfy-pin]=2967033970cbb5884651c9565265778482d3b44e
  value[lifecycle-pin]=96360a27a53c4608f780f7b370f747a2aa3c7ca8
  value[runpod-sdk-pin]=0786b3d51855a66176fea1e7443b92e7ef2d01b5
  versions=(wheel=0.46.3 build=1.4.0 pytest=9.0.2 httpx=0.28.1 filelock=3.32.4 Pillow=12.3.0 setuptools=80.9.0 hatchling=1.27.0 setuptools-scm=8.3.1)
  distributions=(astrid=0.1.0 astrid-runtime=0.1.0)
fi
required=(python agent-root agent-pin runtime-root runtime-pin vibecomfy-root vibecomfy-pin sdk-root sdk-pin lifecycle-root)
if ((locked)); then required+=(lifecycle-pin runpod-sdk-root runpod-sdk-pin); fi
for key in "${required[@]}"; do
  [[ -n ${value[$key]:-} ]] || { printf 'Missing required option: --%s\n' "$key" >&2; usage >&2; exit 2; }
done
for name in wheel build pytest httpx; do
  found=0
  for item in "${versions[@]}"; do [[ ${item%%=*} == "$name" ]] && found=1; done
  ((found)) || { printf 'Missing required --version %s=...\n' "$name" >&2; usage >&2; exit 2; }
done
((${#nodes[@]})) || { printf 'At least one --node is required\n' >&2; usage >&2; exit 2; }
((${#distributions[@]})) || { printf 'At least one --distribution NAME=VERSION is required\n' >&2; usage >&2; exit 2; }

tmp_root=${TMPDIR:-}
[[ -n $tmp_root && -d $tmp_root ]] || { printf 'TMPDIR must name an existing task-local directory\n' >&2; exit 2; }
tmp_root=$(cd "$tmp_root" && pwd -P)
run_tmp=$(mktemp -d "$tmp_root/pinned-env.XXXXXX") || exit 2
base_tmp="$run_tmp/pytest-basetemp"
mkdir "$base_tmp"
trap 'rm -rf -- "$run_tmp"' EXIT HUP INT TERM

declare -a labels=() results=() details=()
add_row() { labels+=("$1"); results+=("$2"); details+=("$3"); }

python_bin=${value[python]}
py() { "$python_bin" -P "$@"; }
if [[ -x $python_bin ]]; then
  py_identity=$(py -c 'import os,sys; print(os.path.realpath(sys.executable))' 2>/dev/null || true)
  expected_identity=$(readlink -f -- "$python_bin" 2>/dev/null || true)
  if [[ -n $py_identity && $py_identity == "$expected_identity" ]]; then
    py_version=$(py -c 'import platform; print(platform.python_version())' 2>/dev/null || echo unknown)
    add_row interpreter PASS "$py_identity (Python $py_version)"
  else
    add_row interpreter FAIL "could not resolve interpreter $python_bin"
  fi
else
  add_row interpreter FAIL "not executable: $python_bin"
fi

check_root_pin() {
  local label=$1 root=$2 pin=$3 actual
  if [[ ! -d $root ]]; then add_row "$label" FAIL "missing root: $root"; return; fi
  actual=$(git -C "$root" rev-parse HEAD 2>/dev/null || true)
  if [[ -z $actual ]]; then add_row "$label" FAIL "not a readable Git checkout: $root"; return; fi
  if [[ $actual == "$pin" ]]; then add_row "$label" PASS "$root @ $actual";
  else add_row "$label" FAIL "$root @ $actual; expected $pin"; fi
  if ((locked)); then
    if git -C "$root" diff --quiet HEAD -- && git -C "$root" diff --cached --quiet --; then
      add_row "clean:$label" PASS "tracked files match HEAD"
    else add_row "clean:$label" FAIL "tracked files differ from HEAD"; fi
  fi
}

check_root_pin agent "${value[agent-root]}" "${value[agent-pin]}"
check_root_pin runtime "${value[runtime-root]}" "${value[runtime-pin]}"
check_root_pin vibecomfy "${value[vibecomfy-root]}" "${value[vibecomfy-pin]}"
check_root_pin sdk "${value[sdk-root]}" "${value[sdk-pin]}"
if ((locked)); then
  check_root_pin lifecycle "${value[lifecycle-root]}" "${value[lifecycle-pin]}"
  check_root_pin runpod-sdk "${value[runpod-sdk-root]}" "${value[runpod-sdk-pin]}"
  if [[ $(realpath "${value[sdk-root]}") == "$(realpath "${value[runtime-root]}/packages/python")" ]]; then
    add_row sdk-root PASS "Runtime packages/python selected"
  else add_row sdk-root FAIL "SDK root must be Runtime packages/python"; fi
  actual_version=$(py -c 'import platform; print(platform.python_version())' 2>/dev/null || true)
  if [[ $actual_version == 3.13.17 ]]; then add_row python-version PASS "$actual_version";
  else add_row python-version FAIL "$actual_version; expected 3.13.17"; fi
  size=$(du -sk "${value[locked-env]}" 2>/dev/null | cut -f1)
  if [[ $size =~ ^[0-9]+$ ]] && ((size <= 716800)); then add_row env-size PASS "$size KiB (limit 716800)";
  else add_row env-size FAIL "${size:-unknown} KiB (limit 716800)"; fi
  prefix=$(py -c 'import sys; print(sys.prefix)' 2>/dev/null || true)
  if [[ -n $prefix && $(realpath "$prefix") == "$(realpath "${value[locked-env]}")" ]]; then
    add_row env-prefix PASS "$prefix"
  else add_row env-prefix FAIL "interpreter prefix $prefix differs from locked environment"; fi
fi

if [[ -d ${value[lifecycle-root]} && -f ${value[lifecycle-root]}/pyproject.toml ]]; then
  add_row lifecycle-root PASS "${value[lifecycle-root]} (pyproject.toml present)"
else
  add_row lifecycle-root FAIL "missing lifecycle root/pyproject.toml: ${value[lifecycle-root]}"
fi

agent_lock_pin=$(py - "${value[agent-root]}/runtime.lock.json" <<'PY' 2>/dev/null || true
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    print(data["source_commit"] if "source_commit" in data else data.get("source_commit", ""))
except Exception:
    print("")
PY
)
if [[ -z $agent_lock_pin ]]; then
  add_row agent-lock FAIL "cannot read source_commit from ${value[agent-root]}/runtime.lock.json"
elif [[ $agent_lock_pin == "${value[runtime-pin]}" ]]; then
  add_row agent-lock PASS "runtime.lock.json source_commit=$agent_lock_pin"
else
  add_row agent-lock FAIL "runtime.lock.json source_commit=$agent_lock_pin; expected runtime pin ${value[runtime-pin]}"
fi

export PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 PYTHONNOUSERSITE=1
if ((locked)); then
  export PYTHONPATH="${value[sdk-root]}:${value[runtime-root]}:${value[agent-root]}:${value[vibecomfy-root]}:${value[lifecycle-root]}/src:${value[runpod-sdk-root]}"
else
  export PYTHONPATH="${value[sdk-root]}:${value[runtime-root]}:${value[agent-root]}:${value[vibecomfy-root]}:${value[lifecycle-root]}/src${PYTHONPATH:+:$PYTHONPATH}"
fi
import_specs=("astrid=${value[agent-root]}" "astrid_runtime=${value[runtime-root]}" "astrid_runtime_client=${value[sdk-root]}" "vibecomfy=${value[vibecomfy-root]}" "runpod_lifecycle=${value[lifecycle-root]}/src")
if ((locked)); then import_specs+=("runpod=${value[runpod-sdk-root]}"); fi
for spec in "${import_specs[@]}"; do
  module=${spec%%=*}; expected=${spec#*=}
  detail=$(py - "$module" "$expected" <<'PY' 2>&1
import importlib, pathlib, sys
module, expected = sys.argv[1:]
try:
    loaded = importlib.import_module(module)
    origin = pathlib.Path(loaded.__file__).resolve()
    origin.relative_to(pathlib.Path(expected).resolve())
    print(origin)
except Exception as exc:
    print(f"{type(exc).__name__}: {exc}")
    raise SystemExit(1)
PY
)
  if [[ $? -eq 0 ]]; then add_row "import:$module" PASS "$detail";
  else add_row "import:$module" FAIL "${detail//$'\n'/ }"; fi
done

if ((locked)); then
  detail=$(py - "${value[agent-root]}" "${value[runtime-root]}" "${value[sdk-root]}" "${value[runpod-sdk-root]}" <<'PYCHILD' 2>&1
import os, pathlib, subprocess, sys
agent, runtime, sdk, runpod = sys.argv[1:]
code = """import importlib, pathlib, sys
for name, root in [('astrid', sys.argv[1]), ('runtime_protocol', sys.argv[2]), ('astrid_runtime_client', sys.argv[3]), ('runpod', sys.argv[4])]:
    origin = pathlib.Path(importlib.import_module(name).__file__).resolve()
    origin.relative_to(pathlib.Path(root).resolve())
    print(f'{name}={origin}')
"""
subprocess.run([sys.executable, '-c', code, agent, runtime, sdk, runpod], cwd=agent, check=True)
PYCHILD
  )
  if [[ $? == 0 ]]; then add_row child-import PASS "${detail//$'\n'/; }"; else add_row child-import FAIL "${detail//$'\n'/ }"; fi
  lockdir="$(cd "$(dirname "$0")" && pwd -P)/testenv"
  detail=$(py - "$lockdir" <<'PYLOCK' 2>&1
import importlib.metadata as md, pathlib, re, sys
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
root = pathlib.Path(sys.argv[1]); expected = {}
for path in (root/'proof.lock', root/'build.lock', root/'extra.lock', root/'sdk.lock'):
    for line in path.read_text().splitlines():
        if not re.match(r'^[A-Za-z0-9][A-Za-z0-9_.-]*==', line): continue
        req = Requirement(line.removesuffix(' \\').strip())
        if req.marker and not req.marker.evaluate(): continue
        version = next(iter(req.specifier)).version
        key = canonicalize_name(req.name)
        if key in expected and expected[key] != version: raise ValueError(f'conflicting lock: {key}')
        expected[key] = version
for name, version in expected.items():
    dist = md.distribution(name)
    if dist.version != version: raise ValueError(f'{name}: {dist.version} != {version}')
    pathlib.Path(dist._path).resolve().relative_to(pathlib.Path(sys.prefix).resolve())
for name in ('astrid', 'astrid-runtime'):
    dist = md.distribution(name)
    pathlib.Path(dist._path).resolve().relative_to(pathlib.Path(sys.prefix).resolve())
print(f'{len(expected)} locked distributions and project metadata inside {sys.prefix}')
PYLOCK
  )
  if [[ $? == 0 ]]; then add_row locked-metadata PASS "$detail"; else add_row locked-metadata FAIL "${detail//$'\n'/ }"; fi
fi

for item in "${versions[@]}"; do
  name=${item%%=*}; expected=${item#*=}
  detail=$(py - "$name" "$expected" <<'PY' 2>&1
import importlib.metadata as metadata
import sys
name, expected = sys.argv[1:]
try:
    dist = metadata.distribution(name)
    actual = dist.version
    location = str(dist.locate_file(""))
except metadata.PackageNotFoundError:
    actual, location = "ABSENT", "no installed distribution metadata"
if actual != expected:
    print(f"{name}: installed={actual}, expected={expected}; {location}")
    raise SystemExit(1)
print(f"{name}={actual}; {location}")
PY
)
  if [[ $? -eq 0 ]]; then add_row "dist:$name" PASS "$detail";
  else add_row "dist:$name" FAIL "${detail//$'\n'/ }"; fi
done

for item in "${distributions[@]}"; do
  name=${item%%=*}; expected=${item#*=}
  detail=$(py - "$name" "$expected" <<'PY' 2>&1
import importlib.metadata as metadata
import sys
name, expected = sys.argv[1:]
try:
    dist = metadata.distribution(name)
    actual = dist.version
    location = str(dist.locate_file(""))
except metadata.PackageNotFoundError:
    actual, location = "ABSENT", "no installed distribution metadata"
if actual != expected:
    print(f"{name}: installed={actual}, expected={expected}; {location}")
    raise SystemExit(1)
print(f"{name}={actual}; {location}")
PY
)
  if [[ $? -eq 0 ]]; then add_row "metadata:$name" PASS "$detail";
  else add_row "metadata:$name" FAIL "${detail//$'\n'/ }"; fi
done

if ((locked)); then
  for i in "${!nodes[@]}"; do [[ ${nodes[$i]} == /* ]] || nodes[$i]="${value[agent-root]}/${nodes[$i]}"; done
  cd "$run_tmp" || add_row test-nodes FAIL "cannot enter temporary root"
else
  cd "${value[agent-root]}" || add_row test-nodes FAIL "cannot enter Agent root"
fi
collect_output="$run_tmp/pytest-collect.log"
if [[ -d ${value[agent-root]} ]]; then
  if py -m pytest --collect-only -q -o addopts= --rootdir="${value[agent-root]}" --basetemp="$base_tmp" "${nodes[@]}" >"$collect_output" 2>&1; then
    count=$(rg -c '::' "$collect_output" 2>/dev/null | awk -F: '{n+=$NF} END {print n+0}')
    add_row test-nodes PASS "$count selected node(s) collected; tests not executed"
  else
    detail=$(tail -n 3 "$collect_output" | tr '\n' ' ')
    add_row test-nodes FAIL "pytest --collect-only failed: ${detail:-no diagnostic}"
  fi
fi

printf '%-22s %-6s %s\n' CHECK RESULT DETAIL
printf '%-22s %-6s %s\n' '----------------------' '------' '------'
failed=0
for i in "${!labels[@]}"; do
  printf '%-22s %-6s %s\n' "${labels[$i]}" "${results[$i]}" "${details[$i]}"
  [[ ${results[$i]} == PASS ]] || failed=1
done
exit "$failed"
