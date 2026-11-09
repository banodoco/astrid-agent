# Generate Image

**Executor**: `generation.generate_image`  
**Modality**: image (`schema_version: 2`)  
**Status**: implemented (Sprint 02 — v2 model → mode → backend taxonomy)

Generates images from text prompts (or prompt files) using local (vibecomfy),
cloud (fal), or Codex `image_generation` backends.  A single executor
dispatches through `BackendAdapter` (SD-004) — callers pick a model, a mode,
and a backend; the executor does the rest.  **`--mode` is required** (SD-005).

## Starting models (Tier-1)

| Model               | Modes wired       | Local template(s)              | Cloud endpoint(s)                     | Codex |
|---------------------|-------------------|--------------------------------|---------------------------------------|-------|
| `z-image`           | t2i, i2i          | `image/z_image`, `image/z_image_img2img` | `fal-ai/z-image/turbo`, `fal-ai/z-image/turbo/image-to-image` | yes |
| `qwen-image-2512`   | t2i               | `image/qwen_image_2512`       | `fal-ai/qwen-image`                  | yes |
| `qwen-image-edit`   | edit              | `edit/qwen_image_edit`        | `fal-ai/qwen-image-edit`             | yes |
| `seedream-v5-pro`   | edit              | —                             | `bytedance/seedream/v5/pro/edit`      | no |
| `flux-dev`          | t2i, i2i          | — (no local template per SD-001) | `fal-ai/flux/dev`, `fal-ai/flux/dev/image-to-image` | yes |
| `flux-schnell`      | t2i               | — (no local template per SD-001) | `fal-ai/flux/schnell`                | yes |
| `seedvr2-upscaler`  | upscale           | —                             | `fal-ai/seedvr/upscale/image/seamless` | no |

`upscale` mode is prompt-less: it requires `image_ref` plus optional
`upscale_mode` (`factor`/`target`), `upscale_factor` (1-10), `target_resolution`
(`720p`/`1080p`/`1440p`/`2160p`), `noise_scale`, `seed`, and `output_format`.

The `codex` backend is also wired for image modes above. It runs
`codex exec` and forces Codex's built-in `image_generation` tool by prompt
contract. The PNG is expected under `~/.codex/generated_images/<session-id>/`
as `ig_*.png`; the adapter copies fresh files into `{out}/images` with
deterministic `codex_NNN.png` names. If Codex reports success but no fresh
`ig_*.png` appears, the executor hard-fails.

All entries are registered in `astrid/core/model_catalog/models.yaml` under
`schema_version: 2`.  Each entry declares per-mode `supports: [...]`,
`requires: [...]`, and per-backend `param_map` entries (SD-003).

## Execution modes

### Local (`--execution local`)

Dispatches through `VibeComfyBackend` adapter (SD-004).  Uses
[vibecomfy](https://github.com/nosresearch/vibecomfy) to drive a local
ComfyUI backend.  The `vibecomfy` package is **lazy-imported** inside the
adapter — only loaded when `execution=local`.  Cloud-only invocations never
touch the package (SD-009).

Requires a running ComfyUI instance reachable from the executor process
(configured via the vibecomfy package defaults or environment).

### Cloud (`--execution cloud`)

Dispatches through `FalBackend` adapter (SD-004).  Pure HTTP against
[fal.ai](https://fal.ai) using `astrid/core/util/http.py` `HttpClient`.
No fal SDK required (SD-009).

Requires `FAL_KEY` to be resolvable via the candidate-env-file walk
(see `astrid/core/util/secrets.py`).

### Codex (`--execution codex`)

Dispatches through `CodexBackend`.  This path needs **no `OPENAI_API_KEY`** and
does not read OpenAI API key files. It uses the Codex CLI's own ChatGPT auth at
`~/.codex/auth.json`, so the cheap readiness check is:

1. `codex` binary is on `PATH`
2. `~/.codex/auth.json` exists

If `--execution codex` is requested and either check fails, the executor falls
back to `--execution cloud` when the selected model/mode has a cloud backend,
and prints a warning naming the reason. This is a preflight fallback only; a
real Codex generation failure does not silently switch backends.

`--size`, `--quality`, and `--background` are hints for Codex, not structured
API parameters. The adapter folds them into natural language (for example,
wide 3:2, high fidelity, transparent background). Aspect ratio is often
honored, but exact pixels and true alpha are not guaranteed.

## Escape hatch

**For LoRAs, IP-adapter, controlnet, custom samplers, and exotic conditioning,
use `vibecomfy.run` directly.**  The `generation.generate_image` executor is
scoped to *basic generation only* (prompt, negative_prompt, seed, count, size,
single image_ref, strength, guidance_scale, steps).  Everything else belongs in
the escape hatch.

See:
- `astrid/packs/vibecomfy/executors/run/STAGE.md` — VibeComfy workflow runner
- `docs/generation/` — modality contracts, manifest schema, feature list

## SDK quick-start

Use the public SDK; every executor invocation is attached to a project and
project-scoped runs write their outputs under that project's run tree.

```python
import astrid.sdk as sdk
result = sdk.invoke(
    "generation.generate_image",
    kind="executor",
    project="demo",
    inputs={
        "model": "z-image",
        "mode": "t2i",
        "execution": "local",
        "prompt": "a serene mountain lake at dawn",
    },
    dry_run=True,
)
```

## Equivalent SDK calls

Direct module invocation is rejected by the canonical-entrypoint guard; use
the public SDK action path shown here and in the SDK quick-start above.

```python
import astrid.sdk as sdk
result = sdk.invoke(
    "generation.generate_image",
    kind="executor",
    project="demo",
    inputs={
        "model": 'flux-dev',
        "mode": 't2i',
        "execution": 'cloud',
        "prompt": 'a serene mountain lake at dawn',
    },
    dry_run=True,
)
result = sdk.invoke(
    "generation.generate_image",
    kind="executor",
    project="demo",
    inputs={
        "model": 'z-image',
        "mode": 't2i',
        "execution": 'local',
        "prompt": 'a serene mountain lake at dawn',
    },
    dry_run=True,
)
result = sdk.invoke(
    "generation.generate_image",
    kind="executor",
    project="demo",
    inputs={
        "model": 'flux-dev',
        "mode": 'i2i',
        "execution": 'cloud',
        "prompt": 'turn this into a watercolor painting',
        "image_ref": './input.png',
    },
    dry_run=True,
)
result = sdk.invoke(
    "generation.generate_image",
    kind="executor",
    project="demo",
    inputs={
        "model": 'z-image',
        "mode": 'i2i',
        "execution": 'local',
        "prompt": 'turn this into a watercolor painting',
        "image_ref": './input.png',
    },
    dry_run=True,
)
result = sdk.invoke(
    "generation.generate_image",
    kind="executor",
    project="demo",
    inputs={
        "model": 'qwen-image-edit',
        "mode": 'edit',
        "execution": 'cloud',
        "prompt": 'replace the background with a forest',
        "image_ref": './input.png',
    },
    dry_run=True,
)
result = sdk.invoke(
    "generation.generate_image",
    kind="executor",
    project="demo",
    inputs={
        "model": 'flux-schnell',
        "mode": 't2i',
        "execution": 'cloud',
        "prompt": 'cyberpunk city',
        "count": 3,
        "seed": 42,
    },
    dry_run=True,
)
result = sdk.invoke(
    "generation.generate_image",
    kind="executor",
    project="demo",
    inputs={
        "model": 'flux-dev',
        "mode": 't2i',
        "execution": 'codex',
        "prompt": 'a tiny blue teapot',
        "size": '1024x1024',
        "quality": 'low',
    },
    dry_run=True,
)
```

## Prompts file (JSONL)

For batch generation, use `--prompts-file` with a JSONL file (one JSON object
per line).  Each line may override `model`, `seed`, `count`, `size`,
`negative_prompt`, `image_ref`, `strength`, `guidance_scale`, and `steps`.
**Per-entry `model` overrides must include an explicit `mode` field matching
CLI `--mode`** (SD-005, FLAG-004):

```jsonl
{"prompt": "a cat in a spacesuit", "seed": 42, "count": 2}
{"prompt": "a dog on a skateboard", "negative_prompt": "blurry", "size": "1024x1024"}
{"prompt": "replace the background with a forest", "model": "qwen-image-edit", "mode": "edit", "image_ref": "/path/to/source.png"}
```

`--prompt` and `--prompts-file` are mutually exclusive — providing both is
rejected at argparse.

## Validation rules

1. **Missing `--mode`** → rejected at argparse (required argument, SD-005).
2. **Missing `requires`** (e.g. `flux-dev --mode i2i` without `--image-ref`)
   → hard-fail BEFORE any HTTP call or vibecomfy import.
3. **`--execution` must name a registered backend** such as `local`, `cloud`,
   or `codex`.
4. **Unsupported features** (e.g. `--negative-prompt` on `flux-dev --mode t2i` cloud) →
   dropped with a `Warning` in the manifest; never hard-fail (SD-004).
5. **Per-entry mode mismatch**: If a prompts-file entry overrides `model`
   without a matching `mode` field, the entry is rejected (FLAG-004).

## Output

- `{out}/images/` — generated image files (e.g. `0-flux-dev.png`)
- `{out}/manifest.json` — canonical manifest conforming to `docs/generation/20-manifest-schema.md` (v2)

## Design docs

- `docs/generation/00-features.md` — canonical feature list
- `docs/generation/10-registry-schema.md` — model registry schema (v2)
- `docs/generation/20-manifest-schema.md` — manifest JSON shape (v2)
- `docs/generation/30-image-contract.md` — image modality contract with six canonical modes
