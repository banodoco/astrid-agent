# D73 exact-submit recipe for the prepared P4 and final-core rows

This is a manager recipe, not a body request. It captures one exact tuple for
Agent, Runtime, App, and Composition, waits for the four normal checkout
receipts, and renders `tests/suites.conf.candidate` from those receipt paths.
After the receipt paths are captured, no row resolves a moving integration home
or re-resolves Runtime `current`.

The final-core body remains gated by D72 completion and the D59 cut. The
prepared current integrated collection is 8,817 nodes in 392 files. It keeps
the broad live roots and excludes only the three owned P4 HTTP originals; D13
and both D23 deferred selectors remain visible in the collection evidence.

```bash
set -euo pipefail
ORCH=/tmp/claude-0/-home-user-repo/7dc61be1-7066-52e0-a5d0-94f90e61430a/scratchpad/orch
WORKER=$ORCH/workers/agt-F-final-core-prep-1755
EVIDENCE=$WORKER/evidence/final-core-prepared
mkdir -p "$EVIDENCE"

# Capture once before any checkout request. These four values are immutable for
# this candidate; do not replace them with a later read of a normal home.
AGENT_SHA=$(git -C /home/user/work/agent rev-parse HEAD)
RUNTIME_SHA=$(git -C /home/user/work/runtime rev-parse HEAD)
APP_SHA=$(git -C /home/user/work/app rev-parse HEAD)
COMPOSITION_SHA=$(git -C /home/user/work/composition rev-parse HEAD)
for item in "$AGENT_SHA" "$RUNTIME_SHA" "$APP_SHA" "$COMPOSITION_SHA"; do
  [[ "$item" =~ ^[0-9a-f]{40}$ ]]
done

# Existing normal checkout/receipt route. No new pin worktree is requested
# until this exact final tuple is ready.
"$ORCH/bin/request-checkout" agent "$AGENT_SHA" 'F final-core exact Agent pin'
"$ORCH/bin/request-checkout" runtime "$RUNTIME_SHA" 'F final-core exact Runtime pin'
"$ORCH/bin/request-checkout" app "$APP_SHA" 'F final-core exact App pin'
"$ORCH/bin/request-checkout" composition "$COMPOSITION_SHA" 'F final-core exact Composition pin'

receipt_path() {
  local repo=$1 sha=$2 receipt="$ORCH/queue/done/checkout-$1-$2"
  while test ! -s "$receipt"; do sleep 1; done
  test -n "$(awk '/^created / { print $2; exit }' "$receipt")"
  awk '/^created / { print $2; exit }' "$receipt"
}
AGENT_PATH=$(receipt_path agent "$AGENT_SHA")
RUNTIME_PATH=$(receipt_path runtime "$RUNTIME_SHA")
APP_PATH=$(receipt_path app "$APP_SHA")
COMPOSITION_PATH=$(receipt_path composition "$COMPOSITION_SHA")
for pair in \
  "agent:$AGENT_PATH:$AGENT_SHA" "runtime:$RUNTIME_PATH:$RUNTIME_SHA" \
  "app:$APP_PATH:$APP_SHA" "composition:$COMPOSITION_PATH:$COMPOSITION_SHA"; do
  IFS=: read -r name path sha <<<"$pair"
  test "$(git -C "$path" rev-parse HEAD)" = "$sha"
  test -z "$(git -C "$path" status --porcelain --untracked-files=all)"
done

PYTHON=/home/user/work/venv-agent/bin/python
NODE=/home/user/work/toolchain/node-v20.19.4-linux-x64/bin/node
PROFILE=/home/user/work/runtimes/agent-remotion-v3-819acf-1110
SCHEMA=/home/user/work/venv-agent/lib/python3.13/site-packages
ARCHIVE=$($PYTHON -P - "$PROFILE" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
print((p / json.loads((p / 'package.json').read_text())['dependencies']['@banodoco/timeline-composition'].removeprefix('file:')).resolve())
PY
)
CLIENT_VERSION=$($PYTHON -P - "$AGENT_PATH" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from astrid_runtime_client.contract_metadata import CONTRACT_VERSION, SCHEMA_DIGEST
print(CONTRACT_VERSION)
print(SCHEMA_DIGEST)
PY
)
CLIENT_VERSION=$(printf '%s\n' "$CLIENT_VERSION" | sed -n '1p')
SCHEMA_DIGEST=$(printf '%s\n' "$CLIENT_VERSION" "$($PYTHON -P - "$AGENT_PATH" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from astrid_runtime_client.contract_metadata import SCHEMA_DIGEST
print(SCHEMA_DIGEST)
PY
)" | tail -1)
cat > "$EVIDENCE/tuple.env" <<EOF
format=p4-d73-v1
agent_sha=$AGENT_SHA
agent_tree=$(git -C "$AGENT_PATH" rev-parse HEAD^{tree})
agent_parents=$(git -C "$AGENT_PATH" rev-list --parents -n 1 HEAD)
agent_path=$AGENT_PATH
runtime_sha=$RUNTIME_SHA
runtime_tree=$(git -C "$RUNTIME_PATH" rev-parse HEAD^{tree})
runtime_parents=$(git -C "$RUNTIME_PATH" rev-list --parents -n 1 HEAD)
runtime_path=$RUNTIME_PATH
app_sha=$APP_SHA
app_tree=$(git -C "$APP_PATH" rev-parse HEAD^{tree})
app_parents=$(git -C "$APP_PATH" rev-list --parents -n 1 HEAD)
app_path=$APP_PATH
composition_sha=$COMPOSITION_SHA
composition_tree=$(git -C "$COMPOSITION_PATH" rev-parse HEAD^{tree})
composition_parents=$(git -C "$COMPOSITION_PATH" rev-list --parents -n 1 HEAD)
composition_path=$COMPOSITION_PATH
python=$PYTHON
node=$NODE
profile=$PROFILE
schema=$SCHEMA
node_version=$($NODE --version)
profile_lock_sha=$(sha256sum "$PROFILE/package-lock.json" | awk '{print $1}')
profile_archive_sha=$(sha256sum "$ARCHIVE" | awk '{print $1}')
profile_registry_sha=$(sha256sum "$PROFILE/.astrid-registry-state.json" | awk '{print $1}')
composition_version=$(python3 -c 'import json; print(json.load(open("'"$PROFILE"'/node_modules/@banodoco/timeline-composition/package.json"))["version"])')
client_version=$CLIENT_VERSION
schema_digest=$SCHEMA_DIGEST
runtime_lock_files=$(python3 -c 'import json; print(len(json.load(open("'"$AGENT_PATH"'/runtime.lock.json"))["files"]))')
original_p4_test_hash=$(sha256sum "$AGENT_PATH/tests/editing_conformance/test_p4_cli_sheet_v2_runtime.py" "$AGENT_PATH/tests/editing_conformance/test_p4_ten_note_v2_runtime.py" "$AGENT_PATH/tests/editing_conformance/test_p4_note_rerun_v2_runtime.py" | sha256sum | awk '{print $1}')
service_versions=agent:$AGENT_SHA,runtime:$RUNTIME_SHA,node:$($NODE --version),composition:$composition_version,client:$CLIENT_VERSION,schema:$SCHEMA_DIGEST
junit_paths=$EVIDENCE/p4-current-single.xml,$EVIDENCE/p4-current-becfe-http-3legs.xml,$EVIDENCE/final-core.xml
EOF
chmod 600 "$EVIDENCE/tuple.env"

# Collect only on the captured Agent receipt. This is collection evidence, not
# a test-body request. Keep the output, exact selector, and source hashes.
COLLECT="$EVIDENCE/final-core-collect.out"
env -i HOME=/root LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="$AGENT_PATH:$RUNTIME_PATH" \
  ASTRID_RUNTIME_CHECKOUT="$RUNTIME_PATH" \
  ASTRID_RUNTIME_SOURCE_ROOT="$RUNTIME_PATH" \
  ASTRID_TIMELINE_SCHEMA_PYTHONPATH="$SCHEMA" \
  "$PYTHON" -P -m pytest --collect-only -q -o addopts= -p no:cacheprovider \
  --junitxml="$EVIDENCE/final-core-collect.xml" \
  --ignore=tests/editing_conformance/test_p4_cli_sheet_v2_runtime.py \
  --ignore=tests/editing_conformance/test_p4_ten_note_v2_runtime.py \
  --ignore=tests/editing_conformance/test_p4_note_rerun_v2_runtime.py \
  tests/editing_conformance tests/sdk tests/timeline tests/core/timeline \
  tests/core/test_timeline_element_orphan_cleanup.py \
  tests/core/test_timeline_element_publication.py \
  tests/core/test_timeline_element_publication_runtime.py \
  tests/core/rendering/test_timeline_visualize_cli_grammar.py \
  tests/packs/timeline tests/packs/video_editing/test_runtime_publication_chain.py \
  tests/test_timeline_clip_replacement.py > "$COLLECT" 2>&1
grep -F 'tests collected' "$COLLECT" > "$EVIDENCE/final-core-count.txt"
grep '^tests/' "$COLLECT" | sort > "$EVIDENCE/final-core-nodes.txt"
sha256sum "$COLLECT" "$EVIDENCE/final-core-nodes.txt" > "$EVIDENCE/source-collection.sha256"

# Render only the three owned candidate rows. The renderer must reject every
# unresolved token; it never edits the central registry.
python3 - "$EVIDENCE/tuple.env" tests/suites.conf.candidate "$EVIDENCE/suites.conf.d73" <<'PY'
import sys
from pathlib import Path
values = {}
for line in Path(sys.argv[1]).read_text().splitlines():
    if line and not line.startswith('#') and '=' in line:
        key, value = line.split('=', 1)
        values['__P4_D73_' + key.upper() + '__'] = value
values.update({
    '__P4_D73_MANIFEST__': str(Path(sys.argv[1]).resolve()),
    '__P4_D73_EVIDENCE__': str(Path(sys.argv[3]).parent.resolve()),
    '__P4_D73_REQUEST_ID_3LEGS__': 'p4-current-becfe-http-3legs',
    '__P4_D73_REQUEST_ID_SINGLE__': 'p4-current-single',
    '__P4_D73_REQUEST_ID_FINAL_CORE__': 'final-core',
})
text = Path(sys.argv[2]).read_text()
for key, value in values.items():
    text = text.replace(key, value)
if '__P4_D73_' in text:
    raise SystemExit('unresolved D73 render token')
Path(sys.argv[3]).write_text(text)
PY
bash -n "$EVIDENCE/suites.conf.d73"

# Harmless positive dry probe against the already qualified 16328 tuple. COUNT
# is exported into the launcher child; the stub must be called exactly once and
# its exit 17 must survive unchanged. No service body or new checkout occurs.
STUB="$EVIDENCE/harmless-stub.sh"
COUNT="$EVIDENCE/harmless-stub.count"
printf '#!/bin/sh\ntest -n "${COUNT:-}"\nprintf "called\\n" >> "$COUNT"\nexit 17\n' > "$STUB"
chmod 700 "$STUB"
: > "$COUNT"
set +e
COUNT="$COUNT" P4_D73_EVIDENCE="$EVIDENCE" P4_D73_REQUEST_ID=d73-dry-preflight \
  /home/user/work/wt/agt-pin-f4d6c72c/tools/p4-d73-pinned-launch.sh \
  "$ORCH/design/handoff-agt-P4-D73-16328-tuple.env" dry -- "$STUB"
DRY_RC=$?
set -e
test "$DRY_RC" = 17
test "$(wc -l < "$COUNT")" = 1

# Poisoning the exact Agent SHA must fail before the stub is reached.
POISON="$EVIDENCE/poisoned.env"
sed 's/^agent_sha=.*/agent_sha=0000000000000000000000000000000000000000/' \
  "$ORCH/design/handoff-agt-P4-D73-16328-tuple.env" > "$POISON"
: > "$COUNT"
set +e
COUNT="$COUNT" P4_D73_EVIDENCE="$EVIDENCE" P4_D73_REQUEST_ID=d73-poisoned \
  /home/user/work/wt/agt-pin-f4d6c72c/tools/p4-d73-pinned-launch.sh \
  "$POISON" dry -- "$STUB"
POISON_RC=$?
set -e
test "$POISON_RC" != 0
test "$(wc -l < "$COUNT")" = 0

# These are the future body requests, intentionally not submitted by this
# preparation task. The normal central installer may CAS only these owned rows
# and must leave every outside row byte-identical with its 0600 UTC backup:
#   R=$("$ORCH/bin/request-test" integration:agent p4-current-single)
#   R=$("$ORCH/bin/request-test" integration:agent p4-current-becfe-http-3legs)
#   R=$("$ORCH/bin/request-test" integration:agent final-core)
```

The final manager rebind is this same bounded sequence: capture four SHAs once,
verify receipt SHA/tree/parents/clean state, verify the 15-file Runtime lock,
contract client `0.2.14` / schema `sha256:867f3553…`, existing profile
`819acf6…`, and Node `v20.19.4`, then render and retain the manifest, collection
hashes, command, logs, JUnit, and exits. It does not chase a moving head.
