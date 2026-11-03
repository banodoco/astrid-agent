# 038_A07_GUIDANCE_DRAFT report

## Result

Updated the three requested guidance pages on branch `G/038-a07-guidance-draft`,
based on Agent `ddd5d278` with the RunPod behavior reviewed at A10 lane
`edabe54f`. No code, tests, fixtures, or unrelated documentation changed.

Changed files:

- `astrid/agent_profiles/astrid.md`
- `astrid/packs/_core/docs/SKILL.md`
- `astrid/packs/generation/docs/SKILL.md`

The generation guide now has attached and managed Comfy dry-runs, readiness
repair guidance, task follow and artifact inspection, per-call RunPod consent
and bounds, warm managed release, and A10 manual-start recovery instructions.
The profile and core skill route catalog generation to that guide and keep
saved-workflow guidance on the VibeComfy route.

## CLI commands and results

Commands below ran from
`/home/user/astrid/proof-codex-gen/work/038/astrid-agent`. The established
interpreter is
`/home/user/astrid/proof-codex/work/21/python-env/bin/python`.

### Installed CLI mismatch

Without a `PYTHONPATH` override, the established interpreter could not load
the installed CLI because its environment lacks `runtime_protocol`. No package
was installed. The generation commands each exited 1 with empty stdout and the
missing-module diagnostic shown below:

```text
/home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate --help
/home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate list --help
/home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate describe --help
/home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate targets --help
/home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate release --help
/home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid tasks --help
```

```text
unstructured - this is a bug.
No module named 'runtime_protocol'
fix: retry the command; if it repeats, report this bug
recovery: retry the command; if it repeats, report this bug
state snapshot: {"argv": [...], "entrypoint": "astrid.core.gateway.main", "original_type": "ModuleNotFoundError"}
```

`/home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid tasks
--help` printed the task-family help and exited 0.

The additional installed list probes also exited 1 with that diagnostic:

```text
/home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate list --help
/home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate list --kind image --json
```

Both exited 1 with the same diagnostic and empty stdout.

An initial task-source-only `PYTHONPATH="$PWD"` batch, without the sibling
runtime/VibeComfy source paths, also failed on the missing module. Each command
exited 1 with empty stdout and that diagnostic:

```text
PYTHONPATH="$PWD" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate --help
PYTHONPATH="$PWD" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate list --help
PYTHONPATH="$PWD" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate describe --help
PYTHONPATH="$PWD" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate target --help
PYTHONPATH="$PWD" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate task --help
PYTHONPATH="$PWD" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate engine --help
```

### Task-head CLI help and read-only probes

To inspect the exact base checkout without installing anything, the shared
interpreter used this task worktree and the already-present sibling source
paths:

```text
PYTHONPATH="$PWD:/home/user/astrid/proof-codex-gen/src/astrid-runtime:/home/user/astrid/proof-codex-gen/src/VibeComfy" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate --help
PYTHONPATH="$PWD:/home/user/astrid/proof-codex-gen/src/astrid-runtime:/home/user/astrid/proof-codex-gen/src/VibeComfy" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate list --help
PYTHONPATH="$PWD:/home/user/astrid/proof-codex-gen/src/astrid-runtime:/home/user/astrid/proof-codex-gen/src/VibeComfy" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate describe --help
PYTHONPATH="$PWD:/home/user/astrid/proof-codex-gen/src/astrid-runtime:/home/user/astrid/proof-codex-gen/src/VibeComfy" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate targets --help
PYTHONPATH="$PWD:/home/user/astrid/proof-codex-gen/src/astrid-runtime:/home/user/astrid/proof-codex-gen/src/VibeComfy" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate release --help
PYTHONPATH="$PWD:/home/user/astrid/proof-codex-gen/src/astrid-runtime:/home/user/astrid/proof-codex-gen/src/VibeComfy" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid tasks follow --help
```

Each exited 0 with empty stderr. The help lists `generate {list,image,video,
describe,workflow,targets,release}`; generation supports `--target`,
`--dry-run`, `--consent {user_env,paid}`, `--max-usd`, `--deadline`, and
`--no-wait`. Target help supports `--target`, `--engine`, `--save`, `--input`,
and `--json`. Task follow requires a project and task ID.

Further help probes each exited 0 with empty stderr:

```text
PYTHONPATH="$PWD:/home/user/astrid/proof-codex-gen/src/astrid-runtime:/home/user/astrid/proof-codex-gen/src/VibeComfy" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate image --help
PYTHONPATH="$PWD:/home/user/astrid/proof-codex-gen/src/astrid-runtime:/home/user/astrid/proof-codex-gen/src/VibeComfy" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid tasks show --help
PYTHONPATH="$PWD:/home/user/astrid/proof-codex-gen/src/astrid-runtime:/home/user/astrid/proof-codex-gen/src/VibeComfy" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid tasks events --help
```

Stdout identifies the image selectors (`--entry`, `--model`, `--mode`),
target and dry-run controls; `tasks show` and `tasks events` take `--project`,
the task ID, and optional `--json`.

The managed-install recovery surface was also checked; both commands exited 0
with empty stderr:

```text
PYTHONPATH="$PWD:/home/user/astrid/proof-codex-gen/src/astrid-runtime:/home/user/astrid/proof-codex-gen/src/VibeComfy" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid engines --help
PYTHONPATH="$PWD:/home/user/astrid/proof-codex-gen/src/astrid-runtime:/home/user/astrid/proof-codex-gen/src/VibeComfy" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid engines install --help
```

Stdout lists only the `install` and `status` engine commands; install accepts
`--platform`, `--placement {local,runpod}`, `--root`, `--deadline`,
`--dry-run`, and model-cache controls.

```text
PYTHONPATH="$PWD:/home/user/astrid/proof-codex-gen/src/astrid-runtime:/home/user/astrid/proof-codex-gen/src/VibeComfy" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate list --kind image --json
```

Exit 0, empty stderr. Stdout is one JSON array of image route/readiness rows;
the environment had no attached ComfyUI, so local rows reported
`ready:false`, `reason:"no attached ComfyUI target or model probe unavailable"`
and `fix:"Start ComfyUI on loopback and make /object_info available."` The
inventory includes `z-image` for `t2i` and `i2i`.

```text
PYTHONPATH="$PWD:/home/user/astrid/proof-codex-gen/src/astrid-runtime:/home/user/astrid/proof-codex-gen/src/VibeComfy" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate describe --entry z-image --mode t2i --json
```

Exit 0, empty stderr. Stdout is JSON describing `z-image` as an available
image `t2i` entry, with declared inputs including `prompt`, `seed`, `steps`,
`diffusion_model`, `text_encoder`, and `vae`.

```text
PYTHONPATH="$PWD:/home/user/astrid/proof-codex-gen/src/astrid-runtime:/home/user/astrid/proof-codex-gen/src/VibeComfy" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate targets --target managed --json
```

Exit 0, empty stderr. Stdout: readiness envelope `status:"ready"`, owner
`astrid`, endpoint `null`. This inspects selection only; it does not establish
that the managed runtime is installed or healthy.

```text
PYTHONPATH="$PWD:/home/user/astrid/proof-codex-gen/src/astrid-runtime:/home/user/astrid/proof-codex-gen/src/VibeComfy" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate targets --target http://127.0.0.1:8188 --json
```

Exit 0, empty stderr. Stdout: readiness envelope `status:"ready"`, owner
`human`, endpoint `http://127.0.0.1:8188`. As above, target selection is not a
health probe.

```text
PYTHONPATH="$PWD:/home/user/astrid/proof-codex-gen/src/astrid-runtime:/home/user/astrid/proof-codex-gen/src/VibeComfy" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate targets --target runpod --json
```

Exit 0, empty stderr. Stdout: readiness envelope `status:"ready"`,
`preferences:{}`, `target:null`; this base CLI probe does not describe or
contact a retained RunPod worker.

### Dry-runs

```text
PYTHONPATH="$PWD:/home/user/astrid/proof-codex-gen/src/astrid-runtime:/home/user/astrid/proof-codex-gen/src/VibeComfy" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate image "a red glass teapot" --entry z-image --mode t2i --target http://127.0.0.1:8188 --dry-run --json
```

Exit 2. Stdout is a blocked readiness JSON envelope with no task ID. Issue:
`target_unreachable`; message says `/system_stats` failed. Exact read-only fix:
`astrid generate targets --target http://127.0.0.1:8188`, with guidance to
start ComfyUI at that URL or use managed mode with an installed ComfyUI
runtime. Stderr was empty.

```text
PYTHONPATH="$PWD:/home/user/astrid/proof-codex-gen/src/astrid-runtime:/home/user/astrid/proof-codex-gen/src/VibeComfy" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate image "a red glass teapot" --entry z-image --mode t2i --target managed --dry-run --json
```

Exit 2. Stdout is a blocked readiness JSON envelope with no task ID. Issue:
`engine_not_found`; neither `comfy.cmd.main` nor the `comfyui` CLI was
available. The exact `managed_write` fix was
`astrid engines install vibecomfy-h3 --platform linux-x86_64-cu130
--placement local`; its details reported at least 53,484,234,760 bytes of
locked models, additional download size unknown, and user-scope/no system
changes. The second, read-only fix was
`astrid generate image --target http://127.0.0.1:8188`. Neither fix was run.
Stderr was empty.

The first 30-second invocation of each dry-run yielded no captured output;
both were rerun to completion and the results above are from those completed
runs. The first target-list invocation likewise yielded no captured output;
the later inventory probe above completed with exit 0.

## A10 review and execution limits

Reviewed `git diff ddd5d278 edabe54f` for
`astrid/core/generation/cli.py`, `astrid/sdk/generation.py`,
`astrid/core/generation/preflight.py`, and `tests/sdk/test_generation_target.py`.
The A10 diff adds `generate describe --target runpod`, the selected human
Comfy `generate launch` command, and RunPod release delegation. Its tests
require fresh per-call `paid` consent, positive `max_usd`, and a future UTC
deadline before RunPod preparation. RunPod preferences do not provide those
controls. The base parser accepts `--target runpod`, `--consent paid`,
`--max-usd`, and `--deadline`; it does not yet contain A10's `launch` or
RunPod describe parser additions.

This was verified with the task-head command below; it exited 2 and printed
the valid base commands (`list` only in this dispatch path):

```text
PYTHONPATH="$PWD:/home/user/astrid/proof-codex-gen/src/astrid-runtime:/home/user/astrid/proof-codex-gen/src/VibeComfy" /home/user/astrid/proof-codex/work/21/python-env/bin/python -m astrid generate launch --help
```

Therefore A10-only launch and retained RunPod describe/release behavior were
reviewed from the named lane's parser, implementation, and tests, but not
executed against the installed CLI. No live generation, paid RunPod request,
managed install, task follow against a real task, or warm release was run.
There was no available task to inspect for real artifacts.

No tests or pytest were run (docs-only scope).

## Validation and deliverables

- `git diff --check`: passed.
- Changed worktree paths: exactly the three allowed docs listed above.
- Local branch commit: `afde47d3e` (`docs: clarify Phase 1 Comfy generation guidance`).
- Patch: `/home/user/astrid/proof-codex-gen/patches/038_A07_GUIDANCE_DRAFT.patch`
- Handoff: `orchestrator relay a07-draft /home/user/astrid/proof-codex-gen/patches/038_A07_GUIDANCE_DRAFT.patch`

## Manager review and integration

Manager reviewed the patch against the current integration checkout. The original
patch was based on `ddd5d278`; its `_core` routing hunk conflicted with the newer
`where-things-run.md` matrix in the integration checkout. Resolved that hunk by
keeping the current matrix link and applying the new catalog/Comfy/RunPod routing.
The other two files applied cleanly. The integrated three-file diff passed
`git diff --cached --check`; the cached path list contained exactly the three
allowed docs. No pytest was run, per the docs-only acceptance.

Integrated as `68c331263` on `integration`. The handoff patch was regenerated
from that commit at `/home/user/astrid/proof-codex-gen/patches/038_A07_GUIDANCE_DRAFT.patch`.
