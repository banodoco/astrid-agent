#!/bin/bash
# D73 P4 service launcher. The manager renders the manifest and paths once
# after the four normal request-checkout receipts have arrived.
set -euo pipefail

manifest=${1:?usage: p4-d73-pinned-launch.sh MANIFEST MODE -- COMMAND...}
mode=${2:?usage: p4-d73-pinned-launch.sh MANIFEST MODE -- COMMAND...}
shift 2
[ "${1:-}" = -- ] || { echo 'D73 launcher: missing -- before command' >&2; exit 2; }
shift
[ $# -gt 0 ] || { echo 'D73 launcher: command is empty' >&2; exit 2; }
[ -f "$manifest" ] || { echo "D73 launcher: missing manifest $manifest" >&2; exit 2; }
[ "$mode" = dry ] || [ "$mode" = run ] || { echo "D73 launcher: unknown mode $mode" >&2; exit 2; }

declare -A pin
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        ''|'#'*) continue ;;
        *=*) key=${line%%=*}; pin["$key"]=${line#*=} ;;
        *) echo 'D73 launcher: malformed manifest line' >&2; exit 2 ;;
    esac
done < "$manifest"

required=(format agent_sha agent_tree agent_parents agent_path runtime_sha runtime_tree runtime_path app_sha app_tree app_path composition_sha composition_tree composition_path python node profile schema node_version profile_lock_sha profile_archive_sha profile_registry_sha composition_version original_p4_test_hash service_versions junit_paths)
for key in "${required[@]}"; do
    [ -n "${pin[$key]+present}" ] && [ -n "${pin[$key]}" ] || { echo "D73 launcher: missing manifest key $key" >&2; exit 2; }
done
[ "${pin[format]}" = p4-d73-v1 ] || { echo 'D73 launcher: unsupported manifest format' >&2; exit 2; }

sha() { [[ "$1" =~ ^[0-9a-f]{40}$ ]]; }
for key in agent_sha runtime_sha app_sha composition_sha; do
    sha "${pin[$key]}" || { echo "D73 launcher: invalid $key" >&2; exit 2; }
done
for key in agent_tree runtime_tree app_tree composition_tree; do
    [[ "${pin[$key]}" =~ ^[0-9a-f]{40}$ ]] || { echo "D73 launcher: invalid $key" >&2; exit 2; }
done
for key in agent_path runtime_path app_path composition_path; do
    [[ "${pin[$key]}" = /* ]] || { echo "D73 launcher: $key is not absolute" >&2; exit 2; }
    [ "$(realpath -e "${pin[$key]}")" = "${pin[$key]}" ] || { echo "D73 launcher: $key is not the exact checkout path" >&2; exit 2; }
done
test "$(realpath -e "$(dirname "$0")")" = "${pin[agent_path]}/tools" || { echo 'D73 launcher: launcher is not from the pinned Agent checkout' >&2; exit 1; }

for pair in agent runtime app composition; do
    path=${pin[${pair}_path]}
    expected=${pin[${pair}_sha]}
    tree=${pin[${pair}_tree]}
    test "$(git -C "$path" rev-parse HEAD)" = "$expected" || { echo "D73 launcher: $pair SHA mismatch" >&2; exit 1; }
    test "$(git -C "$path" rev-parse HEAD^{tree})" = "$tree" || { echo "D73 launcher: $pair tree mismatch" >&2; exit 1; }
    test -z "$(git -C "$path" status --porcelain --untracked-files=all)" || { echo "D73 launcher: $pair checkout is dirty" >&2; exit 1; }
done
test "$(git -C "${pin[agent_path]}" rev-list --parents -n 1 HEAD)" = "${pin[agent_parents]}" || { echo 'D73 launcher: Agent parent guard mismatch' >&2; exit 1; }
for pair in agent runtime app composition; do
    parent_key=${pair}_parents
    if [ -n "${pin[$parent_key]+present}" ]; then
        test "$(git -C "${pin[${pair}_path]}" rev-list --parents -n 1 HEAD)" = "${pin[$parent_key]}" || { echo "D73 launcher: $pair parent guard mismatch" >&2; exit 1; }
    fi
done
test "$(sha256sum "${pin[agent_path]}/tests/editing_conformance/test_p4_cli_sheet_v2_runtime.py" "${pin[agent_path]}/tests/editing_conformance/test_p4_ten_note_v2_runtime.py" "${pin[agent_path]}/tests/editing_conformance/test_p4_note_rerun_v2_runtime.py" | sha256sum | awk '{print $1}')" = "${pin[original_p4_test_hash]}" || { echo 'D73 launcher: original P4 test hash mismatch' >&2; exit 1; }

evidence=${P4_D73_EVIDENCE:?D73 launcher: P4_D73_EVIDENCE is required}
request_id=${P4_D73_REQUEST_ID:?D73 launcher: P4_D73_REQUEST_ID is required}
mkdir -p "$evidence"
cp "$manifest" "$evidence/$request_id.manifest"
printf '%s\n' "$request_id" > "$evidence/$request_id.request-id"
{
    printf 'agent_sha=%s\n' "${pin[agent_sha]}"
    printf 'runtime_sha=%s\n' "${pin[runtime_sha]}"
    printf 'app_sha=%s\n' "${pin[app_sha]}"
    printf 'composition_sha=%s\n' "${pin[composition_sha]}"
    printf 'original_p4_test_hash=%s\n' "${pin[original_p4_test_hash]}"
    printf 'service_versions=%s\n' "${pin[service_versions]}"
    printf 'junit_paths=%s\n' "${pin[junit_paths]}"
} > "$evidence/$request_id.runmanifest"

preflight() {
    test -x "${pin[python]}"
    test -x "${pin[node]}"
    test -d "${pin[schema]}"
    test -d "${pin[profile]}/node_modules"
    test ! -L "${pin[profile]}/node_modules"
    test "$(sha256sum "${pin[profile]}/package-lock.json" | awk '{print $1}')" = "${pin[profile_lock_sha]}"
    package_archive=$("${pin[python]}" -P - "${pin[profile]}" <<'PY'
import json
import sys
from pathlib import Path
profile = Path(sys.argv[1])
package = json.loads((profile / 'package.json').read_text())
print((profile / package['dependencies']['@banodoco/timeline-composition'].removeprefix('file:')).resolve())
PY
)
    test "$(sha256sum "$package_archive" | awk '{print $1}')" = "${pin[profile_archive_sha]}"
    test "$(sha256sum "${pin[profile]}/.astrid-registry-state.json" | awk '{print $1}')" = "${pin[profile_registry_sha]}"
    test "$("${pin[node]}" --version)" = "${pin[node_version]}"
    "${pin[python]}" -P - "${pin[agent_path]}" "${pin[runtime_path]}" "${pin[runtime_sha]}" "${pin[schema]}" "${pin[profile]}" "${pin[composition_version]}" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

agent, runtime, runtime_sha, schema_root, profile, composition_version = map(Path, sys.argv[1:])
lock = json.loads((agent / 'runtime.lock.json').read_text())
assert lock['source_commit'] == runtime_sha.name
assert lock['producer'] == 'astrid-runtime'
assert len(lock['files']) == 15
for rel, entry in lock['files'].items():
    assert hashlib.sha256((agent / rel).read_bytes()).hexdigest() == entry['sha256'].removeprefix('sha256:')
import astrid
import astrid_runtime_client
from astrid_runtime_client.contract_metadata import CONTRACT_VERSION, SCHEMA_DIGEST
import astrid.core.timeline.banodoco_schema as schema
import banodoco_timeline_schema as shared_schema
import runtime_protocol
assert Path(astrid.__file__).resolve() == agent / 'astrid/__init__.py'
assert Path(astrid_runtime_client.__file__).resolve() == agent / 'astrid_runtime_client/__init__.py'
assert Path(schema.__file__).resolve() == agent / 'astrid/core/timeline/banodoco_schema.py'
assert Path(runtime_protocol.__file__).resolve() == runtime / 'runtime_protocol/__init__.py'
assert Path(shared_schema.__file__).resolve() == schema_root / 'banodoco_timeline_schema/__init__.py'
assert CONTRACT_VERSION == '0.2.14'
assert SCHEMA_DIGEST == 'sha256:867f3553ac0ff264300e5a2596b363cdb4c071842eb6570ad461c093d351708b'
package = json.loads((profile / 'node_modules/@banodoco/timeline-composition/package.json').read_text())
assert package['version'] == composition_version.name
PY
}

preflight_log="$evidence/$request_id.preflight.log"
set +e
(
    unset PYTHONNOUSERSITE
    export PYTHONPATH="${pin[agent_path]}:${pin[runtime_path]}"
    export ASTRID_RUNTIME_CHECKOUT="${pin[runtime_path]}"
    export ASTRID_RUNTIME_SOURCE_ROOT="${pin[runtime_path]}"
    export ASTRID_REMOTION_PROJECT_DIR="${pin[profile]}"
    export ASTRID_NODE_EXECUTABLE="${pin[node]}"
    export ASTRID_TEST_NODE_MODULES="${pin[profile]}/node_modules"
    cd "${pin[agent_path]}"
    preflight
) > >(tee "$preflight_log") 2>&1
preflight_rc=$?
set -e
printf '%s\n' "$preflight_rc" > "$evidence/$request_id.preflight.exit"
[ "$preflight_rc" = 0 ] || exit "$preflight_rc"

printf '%q ' "$@" > "$evidence/$request_id.command"
printf '\n' >> "$evidence/$request_id.command"
body_log="$evidence/$request_id.body.log"
body_exit="$evidence/$request_id.body.exit"
set +e
(
    unset PYTHONNOUSERSITE
    export PYTHONPATH="${pin[agent_path]}:${pin[runtime_path]}"
    export ASTRID_RUNTIME_CHECKOUT="${pin[runtime_path]}"
    export ASTRID_RUNTIME_SOURCE_ROOT="${pin[runtime_path]}"
    export ASTRID_REMOTION_PROJECT_DIR="${pin[profile]}"
    export ASTRID_NODE_EXECUTABLE="${pin[node]}"
    export ASTRID_TEST_NODE_MODULES="${pin[profile]}/node_modules"
    cd "${pin[agent_path]}"
    if [ "$mode" = dry ]; then
        "$@"
    else
        "${pin[python]}" -P "$@"
    fi
) > >(tee "$body_log") 2>&1
body_rc=$?
set -e
printf '%s\n' "$body_rc" > "$body_exit"
exit "$body_rc"
