# Generate Image

Primary entry point for standard image generation via the Astrid model
registry.  Dispatches through `BackendAdapter` (SD-004) — local goes to
vibecomfy ready-templates, cloud goes to fal.ai endpoints.

## Codex pixel art for film sprites

For Codex image generation (`execution: "codex"`), state the native pixel
density in the prompt: "each icon about 44 pixels wide, every pixel a perfect
square of identical size". A film with one art-pixel scale gets its on-screen
sprite size from that drawn width. Generate the sheet, then pass its output
handle to [`pixel.sprites`](../../pixel/docs/SKILL.md) to key, snap at
the drawn lattice, split, and preview the sprites. Do not resample a finished
sprite to change its size.

## Quick-start

There is no executor CLI.  Run `generation.generate_image` through the SDK, or
admit it as a kernel task (the pack ships a task adapter) and track it through
the `tasks` / `runs` families:

```python
import astrid.sdk as sdk

result = sdk.invoke(
    "generation.generate_image",
    kind="executor",
    inputs={
        "model": "flux-schnell",   # fast, cheap cloud t2i
        "mode": "t2i",
        "execution": "cloud",
        "prompt": "a serene mountain lake at dawn",
    },
    project="demo",                # every executor run belongs to exactly one project
    wait=True,
)
print(result.ok, result.outputs.get("artifacts", []))
```

```bash
# Task-mode equivalent: admit one immutable task, then watch it run
python3 -m astrid tasks create --project demo \
  --capability generation.generate_image \
  --spec '{"model":"flux-dev","mode":"t2i","execution":"cloud","prompt":"a serene mountain lake at dawn"}' --json
python3 -m astrid runs list --project demo --json
```

**`--mode` is required** (SD-005).  No auto-inference from inputs.

The runtime supplies the executor's private staging directory, publishes
declared media files to its content-addressed store, and records the task and
generation associations. Do not invoke `run.py` with a cwd-relative output
directory or maintain a second workspace cache. A custom filesystem path is
an explicit export after the runtime result has settled.

## Canonical image modes

Six canonical modes (SD-002).  Three are wired in Sprint 2:

| Mode | Description | Wired | Key inputs |
|------|-------------|-------|------------|
| `t2i` | Text-to-image (prompt → image). | ✅ | `--prompt` (required). |
| `i2i` | Image-to-image (prompt + ref image + strength → image). | ✅ | `--prompt`, `--image-ref` (required). `--strength` (optional). |
| `edit` | Instruction-guided edit (prompt = instruction, ref image required). | ✅ | `--prompt`, `--image-ref` (required). NO `negative_prompt`, NO `strength`. |
| `inpaint` | Masked region replacement (prompt + image + mask → image). | ❌ | Future sprint. |
| `outpaint` | Boundary extension (prompt + image + direction/extent → image). | ❌ | Future sprint. |
| `upscale` | Super-resolution (image → larger image). | ✅ | `seedvr2-upscaler` (fal, factor/target modes). |

## Tier-1 model decision tree

```
What do you need?

├─ Fast, cheap iteration (cloud, ~1s)
│  → flux-schnell --mode t2i --execution cloud
│     - guidance_scale always 1.0
│     - 4 steps, very fast
│
├─ Best cloud quality (text-to-image)
│  → flux-dev --mode t2i --execution cloud
│     - 28 steps, higher quality
│     - no negative_prompt, no image_ref in t2i mode
│
├─ Best local quality (text-to-image, open model)
│  → z-image --mode t2i --execution local
│     - Requires running ComfyUI + vibecomfy
│     - Supports negative_prompt, guidance_scale, steps
│
├─ Image-to-image (cloud)
│  → flux-dev --mode i2i --execution cloud
│     - Requires --image-ref
│     - Supports --strength (denoising level)
│
├─ Image-to-image (local)
│  → z-image --mode i2i --execution local
│     - Requires --image-ref
│     - Supports --strength
│
├─ Instruction-guided edit (cloud)
│  → qwen-image-edit --mode edit --execution cloud
│     - --prompt is the edit instruction
│     - Requires --image-ref
│     - NO negative_prompt, NO strength
│
└─ Instruction-guided edit (local)
   → qwen-image-edit --mode edit --execution local
      - Same as cloud but via ComfyUI
```

Model list: inspect the registry through Python —
`ModelRegistry.load_default().list_all()` returns registered models (pass
`include_closed=True` for closed-weight entries), and `.get("<model-id>")`
returns per-mode supports/requires and per-backend templates/endpoints.

## Drop-with-warning behavior

Features that the chosen (model, mode, backend) cell does not support are
**dropped with a warning** in the manifest — never a hard failure (SD-004).

Examples:
- `--negative-prompt` on `flux-dev --mode t2i --execution cloud` → dropped, warning
- `--image-ref` on `flux-dev --mode t2i` → dropped, warning
- `--strength` on `qwen-image-edit --mode edit` → dropped, warning

Missing required features (e.g., `--image-ref` for `--mode i2i`) are a
**hard failure** before any HTTP call or vibecomfy import.

## Escape hatch

**For LoRAs, IP-adapter, controlnet, custom samplers, graph composition,
or any path the registry does not cover, use the `vibecomfy` skill
instead.**  The `generate-image` skill is scoped to standard generation
through the registry.  Everything else goes through the vibecomfy escape
hatch, which gives direct access to ready-templates, custom workflows,
and the full ComfyUI node graph.

See: `vibecomfy` skill, `astrid/packs/vibecomfy/executors/run/STAGE.md`

## Runtime publication controls

The shared facade accepts `variant_of={"generation_id": "...",
"variant_id": "..."}` to append exactly one image to an existing generation.
`primary="preserve"` is the default; `primary="promote"` atomically selects
the new variant under Runtime generation-version CAS. The source variant must
belong to the project and have a managed object. These are publication fields,
not executor inputs, and an ordinary image request still uses
`generation.publish_v1`. The generation pack skill documents the same controls
for video and audio and the boundary with media relations, references, and
timeline replacement.
