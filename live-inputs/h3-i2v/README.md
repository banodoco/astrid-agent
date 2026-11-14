# One H3 image-conditioned live job

Place the real 1280x720 first image at `live-inputs/h3-i2v/first-frame.png`:

```sh
cp /home/user/wt/install-r13b/inputs/edit-src.png live-inputs/h3-i2v/first-frame.png
```

The asset-map value is relative to the asset-map file, independent of checkout
location (`astrid/core/generation/entries.py:667`). Run these commands from the
checkout root, using the orchestrator's installed `astrid` and selected project.

CPU proof command (exit 0):

```sh
astrid generate video --entry h3 --mode i2v --input request=live-inputs/h3-i2v/request.json --input asset_map=live-inputs/h3-i2v/asset-map.json --placement runpod --dry-run
```

Live command, on the orchestrator's existing claim (not executed here):

```sh
astrid generate video --entry h3 --mode i2v --input request=live-inputs/h3-i2v/request.json --input asset_map=live-inputs/h3-i2v/asset-map.json --placement runpod
```

This revision's help uses `--input NAME=VALUE`, not `--request`/`--asset-map`.
It exposes no `--machine` flag. `--placement runpod` uses existing claim custody;
generation takes no rental budget or paid-consent flags. Keep the same workspace
and claim as the other cells. `scripts/live_cells.json` lists five other cells;
this is an additional job. Historical rental flags in Release 13 notes predate
this CLI (`astrid/core/generation/cli.py:191`, `astrid/sdk/generation.py:290`).

Expected: one muxed H.264 MP4, 5 frames, 5/24 seconds (0.2083333333), 24 fps,
1024x576, with generated audio. Five frames is the minimum accepted duration
(`astrid/packs/h3_av/shared/generation.py:195`); fps and resolution are fixed
by this adapter (`generation.py:346`, `generation.py:429`). One sampling step
is the accepted minimum (`shared/request_v2.py:219`); default is eight. This is
a cheapest accepted smoke job, with no inference-quality claim. Live runtime
and peak VRAM are unknown; CPU planning does not establish GPU sufficiency.

The image is ordered reference conditioning (Picture 1), not a guaranteed
pixel-preserved first frame (`generation.py:221`). Timed first-frame anchors
are unsupported by this route. No product changes were made.

Proof source: Agent `65b1d45c6a33b7b6fab0b3f8df3aa3aba5f8963e`, clean starting
tree; pinned Runtime `6855dfa8df65cf8ae4c002b57134d6505493c17c`, VibeComfy
`46251c8cc5565e555356e74e3179223a40f978f7`, Python 3.13.18. Setup was
`bash /home/user/astrid-dev/env/agent-setup.sh`; CLI was the resulting
`/home/user/wt/h3i2v.env/venv/bin/astrid`, with the setup script's isolated HOME,
PATH, PYTHONPATH and dependency checkout variables. A simple 1280x720 PNG was
used only for CPU proof and is not committed. Replacing it changes asset and
compilation digests. The environment was removed after proof.

Observed plan excerpt (JSON fields, not a target approval):

```text
status: ready
issues: []
fixes: []
entry.accepted_inputs: [asset_map, request]
entry.rejected_inputs: []
entry.unbound: []
entry.workflow: h3_av_reference_generation
entry.bindings.reference_0: c79f192155056a24-3d7fa1d53c5434db.png
entry.bindings.steps: 1
entry.bindings.seed: 42
entry.build_plan.output_contract: muxed_av_full_timeline
entry.build_plan.compilation_digest: 2993beb38c3857332a4c76283419a2573bc5ad024f006f314293f92029740c4a
target.placement: runpod
target.state: pending_host_readiness
```

`generate describe h3` exited 0. `generate defaults` was attempted but exited 2:
`No workspace is configured`; the pack declares video.i2v priority 100 in
`astrid/packs/h3_av/extensions/vibecomfy.workflows/h3.yaml:22`. No target was
contacted, no tests or inference ran, and no weights were downloaded.
