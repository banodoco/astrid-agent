# Installed canary / H3 harness

Future R8 canaries must invoke this committed tool. Do not copy or adapt scripts
from prior task evidence. The tool operates on one existing, canonical,
test-owned installation. It never runs an installer, acquires a pod/GPU, changes
product source, edits the shipped capability matrix, or grants canary authority.

Use an explicitly selected CPU Python, from any working directory:

```sh
python /absolute/harness/canary.py inventory \
  --tuple /absolute/task/frozen-tuple.json \
  --task-dir /absolute/task --work-dir /absolute/work
# Only under separate canary authorization, after the ordinary installer:
python /absolute/harness/canary.py qualify \
  --tuple /absolute/task/frozen-tuple.json \
  --task-dir /absolute/task --work-dir /absolute/work
```

The caller owns the existing canonical installer, frozen-source preparation,
review of matrix/asset pins, pinned environment checks, machine resource guards,
heavy-slot locking, and authorization. Invoke this tool while that caller's
required lock/guards still hold. Inventory and qualification each require a fresh
`task/logs/installed-canary` directory; evidence is never overwritten. Use a new
task directory for another run. The tool does not retry a failed stage.

The JSON input has exactly these fields (placeholders must be replaced):

```json
{
  "schema_version": 1,
  "task_dir": "/absolute/task",
  "work_dir": "/absolute/work",
  "sources": {
    "agent_base": {"root": "/absolute/base-agent", "sha": "FULL_LOWERCASE_COMMIT", "tree": "FULL_LOWERCASE_TREE"},
    "agent": {"root": "/absolute/selected-agent", "sha": "FULL_LOWERCASE_COMMIT", "tree": "FULL_LOWERCASE_TREE"},
    "runtime": {"root": "/absolute/runtime", "sha": "FULL_LOWERCASE_COMMIT", "tree": "FULL_LOWERCASE_TREE"},
    "vibecomfy": {"root": "/absolute/vibecomfy", "sha": "FULL_LOWERCASE_COMMIT", "tree": "FULL_LOWERCASE_TREE"},
    "lifecycle": {"root": "/absolute/lifecycle", "sha": "FULL_LOWERCASE_COMMIT", "tree": "FULL_LOWERCASE_TREE"},
    "runpod": {"root": "/absolute/runpod", "sha": "FULL_LOWERCASE_COMMIT", "tree": "FULL_LOWERCASE_TREE"}
  },
  "manifest_sha256": "RAW_SHA256_OF_INSTALLED_MANIFEST",
  "matrix": {
    "shipped_sha256": "RAW_SHA256_OF_SELECTED_AGENT_SHIPPED_MATRIX",
    "selected_path": "/absolute/task/selected-capabilities.json",
    "selected_sha256": "RAW_SHA256_OF_REVIEWED_SELECTED_MATRIX",
    "added_row": {
      "id": "installed_canary.parent", "disposition": "optional",
      "evidence_reason": "Your explicit review reference for this test-only row",
      "adapter_family": "cpu", "resource_keys": ["cpu"]
    }
  },
  "parent_inventory": {
    "actions/parent.py": "e03ad1a3f46f9ee4a9765b199d15d34581f92c1cbe1099f52331d98f8bb22acd",
    "pack.yaml": "d163d0c20efc54dcfdc2f942e5cc4c53a0db42b0905e3bca2838dab5e0789ace"
  }
}
```

Every selected source must be a clean checkout at the full commit and tree.
`agent_base` names an available base commit; it need not be checked out. If the
Agent differs from its base, only the reviewed single-child Runtime lock alignment
is accepted. The Runtime source pin and all vendored lock members must match.
The installed manifest must match all five source commits and its explicit
reviewed digest. Source roots may live outside the work directory; the tool only
reads them and uses Git without a fetch or network operation.

Expected installation layout is derived solely from `--work-dir`:
`install/program/current` selects the only `install/program/versions/<version>`;
that version has `manifest.json`, `profile.json`, `py/bin/python`, and one
`py/lib/python*/site-packages`. The canonical installer receipt is supplied at
`task/logs/installer-exit.json` with an integer `exit`. `install/data/runtime`,
`install/home`, `install/tmp`, `install/xdg-{data,state,config,cache}`, and
`install/bin/uv` belong to this test install. Paths must be absolute and free of
aliases and parent segments. The managed interpreter's bootstrap base must be
under `install/xdg-data/astrid/bootstrap/python`.

`inventory` validates the inputs and installed boundary bytes, then retains the
installer receipt, complete ordinary service logs, selected custody records,
redacted install context, and boundary inventory without importing product APIs.
An invalid or incomplete tuple/path is rejected before creating output or child
processes, apart from read-only local Git checks. Failed installer receipts are
captured before qualification refuses them. An absent or corrupt install cannot
be qualified; retain the ordinary installer's own failure evidence separately.

`qualify` performs that capture, then uses only the selected installed interpreter
with `-I -B` and a declared child environment. Its fixed stages are:

1. Bind all 31 reviewed API shapes without invoking them; check installed origins
   and the real active realm. Pin the tuple and committed driver/asset bytes.
2. Check interpreter, package origins, frozen source bytes and distribution RECORDs;
   run installed CLI help and the four installed composition contracts.
3. Declare the exact two-file test parent inventory and the reviewed selected
   matrix. Preserve boot/source pins, recomputing session before profile digest.
4. Restart the test install. Attempt ready/profile/matrix snapshots, all capability
   pages and complete Runtime/host logs in `finally`, before verification or any
   readiness assertion. Failure still retains available captures and stops the
   selected test install.
5. Require parent and H3 prepare readiness, then run the single real H3 parent/child
   journey with discovered capability digests, production scopes, exact custody,
   project/input/lineage/placement checks, admission/claim receipts and expected
   invalid-input terminal failures. No GPU executor or acquisition target is added.

The shipped matrix remains untouched. The selected copy must preserve every row
and other field and add only the reviewed optional CPU parent row. An unsupported
H3 prepare route refuses during validation. Policy is fixed by the reviewed
journey and selected inputs, not an ambient developer override. No new workflow
engine, authority, ledger or warm-lease policy is introduced.

CPU verification uses existing Python/pytest only; no install/venv is required:

```sh
# Give each run its own launcher, TMPDIR, HOME and basetemp under its task directory.
TMPDIR=/absolute/task/run/tmp HOME=/absolute/task/run/home \
  python -m pytest -q -o addopts= --basetemp=/absolute/task/run/basetemp tests
python -m py_compile canary.py installed_canary/*.py installed_canary/assets/actions/parent.py tests/test_canary.py
```

CPU tests construct real temporary Git sources and filesystem install fixtures.
The qualification controller is real; installed child processes and transport
responses are the fake edges. The signature fixture's function bodies raise if
called. The restart ordering test uses a real CPU subprocess for its service
edge. These tests prove harness orchestration, not installed product composition,
H3 operation, GPU behavior, readiness or canary authorization. Installed contracts
and the real journey remain unproven until separately authorized installed runs.

See [PROVENANCE.md](PROVENANCE.md) for extraction sources and proof limits.
