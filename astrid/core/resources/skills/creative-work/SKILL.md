---
name: creative-work
description: Choose a selected capability for creative production and inspect its declared execution contract.
---

# Creative work

Start with the requested result and inspect the
[selected pack directory](references/packs.md). Read the matching selected
skill before choosing a route. Its manifest and capability guidance own the
qualified ids, inputs, execution requirements, and outputs.

The [capability index](../references/capabilities.md) lists only declarations
from this view's selected packs. An empty view has no optional routes. Do not
infer availability from an old core list, a missing guide, or an ambient skill.
If the requested capability is absent, refuse before acquiring dependencies or
starting a process. Describe the missing capability clearly.

## Resolve the selected contract

Find the component in that pack's `pack.yaml`. Read its declared documentation
resource, commonly `actions/<slug>/STAGE.md`, rather than guessing a checkout
path. Inspect `astrid generate list --json` (optionally `--kind image`,
`--kind video`, or `--kind audio`) before recommending a reinstall or a new
engine. Readiness declarations do not verify model bytes or execution.

## Execution

Use the connected Runtime through Astrid's SDK or product commands. Bind work
to the selected project, preserve task/run references, and inspect Runtime
results and artifacts before claiming completion. Readiness and model bytes
must be verified through the selected capability's contract.

Use Runtime-owned storage and publication. Do not create a workspace-relative
runs directory or download an extra convenience copy. Public writes require
explicit approval of the reviewable payload.

For new reusable tools, follow [Pack builder](../pack-builder/SKILL.md).
