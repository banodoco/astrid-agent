# CP3 locked G test environment

Install this directory beside `pinned-env.sh` as `ops/testenv/`. This profile is
Linux x86-64, Python **3.13.17**, uv **0.11.32**, and the exact CP3 tuple in the
selector. The four requirements files are hash-locked; `proof.lock` and
`build.lock` are copies of the Agent's existing locks. `extra.lock` supplies the
remaining build/test tools, and `sdk.lock` resolves the selected SDK's declared
requirements against the other locks. No GPU framework or model is installed.

Build once into a **new** environment directory, using clean disposable shared
clones at the specified Agent and Runtime commits. The build backends write
`build/` and egg-info into those clones; never point this at a shared checkout.
Scratch must be an existing task-local directory. The script checks free space,
Python/uv versions, commits, tracked changes, hashes, and the 700 MiB limit.
`uv` and Python must already be on PATH. Example, with absolute paths chosen by
the orchestrator:

```sh
mkdir -p "$TASK/build-scratch"
ops/testenv/build.sh /home/user/astrid/ops/testenv/cp3 "$TASK/agent" "$TASK/runtime" "$TASK/build-scratch"
```

Remove build artifacts in the disposable source clones, or use fresh clean source
checkouts for testing. Never relocate the resulting venv. The selector expects
its sibling lock files in `ops/testenv/`. Example preflight (TMPDIR must already
exist and be task-local):

```sh
TMPDIR="$TASK/tmp" ops/pinned-env.sh --locked-env /home/user/astrid/ops/testenv/cp3 \
  --agent-root "$AGENT" --runtime-root "$RUNTIME" \
  --sdk-root "$RUNTIME/packages/python" --vibecomfy-root "$VIBECOMFY" \
  --lifecycle-root "$LIFECYCLE" --runpod-sdk-root "$RUNPOD_SDK" \
  --node 'tests/core/generation/test_discovery_scope.py::test_cli_scope_is_per_dry_run_and_is_reset_on_failure'
```

For a suite, create a fresh empty task-local `RUN_DIR` and use the staged
`run-locked.sh ENV AGENT RUNTIME VIBECOMFY LIFECYCLE RUNPOD_SDK RUN_DIR NODE...`.
It runs preflight first and launches only if it passes, with its own HOME,
TMPDIR, basetemp, safe path order, and exact source roots. Delete RUN_DIR after
recording results. Passing preflight establishes collection, imports, pins and
installed metadata; it does not establish test success. Use exact, clean CP3
checkout roots; tracked modifications are rejected by this profile.

The 041 retry built the final four-lock environment from an absent destination
on its first attempt: 145 locked third-party distributions plus real Agent and
Runtime packages, below 700 MiB. Its initial selector passed all 39 checks with
all seven selected nodes. All seven tests ran once on that same environment:
five passed; the entrypoint and release assertions failed. See the retry task
REPORT and logs for exact results and the proof boundary.

After a wheel-build test, remove its generated `build/` and egg-info directories
from the disposable source clones before the next invocation. Source egg-info
on PYTHONPATH can shadow the environment's installed metadata. The retry's
post-release preflight detected this and blocked pytest until those generated
outputs were removed. Never bypass that metadata check. The selected venv and
locked dependencies need no reinstall for this source-output cleanup.
