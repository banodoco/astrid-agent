# US-2 command count capture

Capture date: 2026-11-22. This is a source-checkout fixture using the real
Runtime daemon and a stand-in attached target; it is not the installed-path
qualification. The installed pack-discovery stage is about 0.5 s.

## Source identity and result

- Agent: `6fcfe555a43263a758112c9179cc27f37bbe657d`, with the new count test and owner-test updates uncommitted at capture; no product files changed.
- Runtime: `3c28211f3cb7ac4867ad744d8cdeeb38dd4484c5`
- VibeComfy: canonical pin in `astrid/packs/runpod/pins.json`
- Python: `3.13.18`
- Result: PASS, `test_fresh_process_command_counts`, two distinct accepted task
  receipts from two fresh CLI subprocesses in the same test process.
- Child import provenance matched the Agent worktree, pinned Runtime checkout,
  and pinned VibeComfy checkout. Request counts below are HTTP calls; client
  timing stages are listed separately.

## Sanitized measured vector, each command

```json
{
  "resolve_entry_inputs": 2,
  "entry_find_entry": 3,
  "entry_ready_entry_rows": 1,
  "entry_complete_ready_entry": 1,
  "entry_load_entry_workflow": 1,
  "model_registry_load": 1,
  "workflow_build": 1,
  "pack_discovery_calls": 15,
  "pack_discovery_full_walks": 8,
  "pack_discovery_cached_hits": 7,
  "runtime_http_requests": {
    "GET /v1/app-state/user/generation": 3,
    "GET /v1/capabilities": 1,
    "GET /v1/health": 1,
    "GET /v1/objects/{digest}": 9,
    "GET /v1/projects/{project}": 2,
    "POST /v1/handshake": 2,
    "POST /v1/projects/{project}/objects": 3,
    "POST /v1/tasks": 1
  },
  "runtime_internal_timing_stages": {
    "Runtime acquisition lock": 1,
    "Runtime connection": 1,
    "Runtime launcher connection": 1
  }
}
```

The two validations have distinct purposes: CLI input validation at
`astrid/core/generation/cli.py:748` and remote-target archive revalidation at
`astrid/sdk/generation.py:2234`. The underlying ready-entry row selection,
completion/materialization, workflow load, registry load, and workflow build
each occurred once. `entry_find_entry` is the measured count of `find_entry`
frames in the entries module, including the resolver method and module helper.

The discovery cache returned seven admitted projections, while eight calls
still performed full admission walks. The fingerprint observer recorded 27
tree fingerprints, 6,165 file hashes, and 629,870,103 bytes hashed per command.
These are informational totals from this source fixture, not asserted
ceilings. Even cache hits re-fingerprint tree contents; freshness coverage
prevents skipping that validation. Plan 81 does not change this behavior; it is
a Plan 82 input.

## Exact command

Run from the Agent worktree with the pinned environment:

```bash
env -i PATH=/opt/node22/bin:/usr/local/bin:/usr/bin:/bin HOME=/root/codex/plan-81/run/worktrees/us-2-count2.env/home TMPDIR=/root/codex/plan-81/run/worktrees/us-2-count2.env/tmp XDG_CACHE_HOME=/root/codex/plan-81/run/worktrees/us-2-count2.env/cache PYTHONPYCACHEPREFIX=/root/codex/plan-81/run/worktrees/us-2-count2.env/cache/pycache PYTHONPATH=/root/codex/plan-81/run/worktrees/r14-runtime-pin:/root/codex/plan-81/run/worktrees/us-2-count2:/root/codex/plan-81/run/worktrees/r14-vibecomfy:/root/codex/plan-81/run/worktrees/r14-runtime-pin/packages/python ASTRID_RUNTIME_CHECKOUT=/root/codex/plan-81/run/worktrees/r14-runtime-pin ASTRID_VIBECOMFY_CHECKOUT=/root/codex/plan-81/run/worktrees/r14-vibecomfy ASTRID_TEST_LIFECYCLE_CHECKOUT=/root/codex/plan-81/run/worktrees/us-2-post69.env/lifecycle PYTEST_ADDOPTS= ASTRID_CI_JOB=lanes-integration /root/codex/plan-81/run/worktrees/us-2-count2.env/venv/bin/python -m pytest -p no:cacheprovider --basetemp=/root/codex/plan-81/run/worktrees/us-2-count2.env/basetemp/capture3 -s -q -rA -m integration tests/integrations/test_command_count_capture.py::test_fresh_process_command_counts
```

Raw output remains at
`/root/codex/plan-81/run/us2-count2-final-capture.log` outside the repository.
