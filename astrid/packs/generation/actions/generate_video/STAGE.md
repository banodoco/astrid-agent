# Generate Video

**Executor**: `generation.generate_video`  
**Modality**: video (`schema_version: 2`)  
**Status**: implemented (Sprint 04 — v2 model → mode → backend taxonomy)

Generates videos from text prompts (or prompt files) using local (vibecomfy)
or cloud (fal) backends.  A single executor dispatches through `BackendAdapter`
(SD-004) — callers pick a model, a mode, and a backend; the executor does
the rest.  **`--mode` is required** (SD-005).

Three video modes are wired this sprint: `t2v` (text-to-video), `i2v`
(image-to-video, first-frame conditioning), and `flf` (first-last-frame
interpolation).  `v2v` and `video-edit` are not wired yet.

## Starting models (Sprint 04)

| Model       | Modes wired          | Local template(s)                          | Cloud endpoint(s)                                   |
|-------------|----------------------|--------------------------------------------|-----------------------------------------------------|
| `wan-2.2`   | t2v (cloud-only), i2v, flf (cloud-only) | `video/wan22_i2v_comfy_lightx2v` | `fal-ai/wan/v2.2-a14b/text-to-video/turbo`, `fal-ai/wan/v2.2-a14b/image-to-video/turbo` |
| `ltx-2.3`   | t2v, i2v, flf (local-only) | `video/ltx2_3_t2v`, `video/ltx2_3_i2v`, `video/ltx2_3_runexx_first_last_frame` | `fal-ai/ltx-2.3/text-to-video/fast`, `fal-ai/ltx-2.3/image-to-video/fast` |

All entries are registered in `astrid/core/model_catalog/models.yaml` under
`schema_version: 2`.  Each entry declares per-mode `supports: [...]`,
`requires: [...]`, and per-backend `param_map` entries (SD-003).

### Wired cells summary

| Model     | Mode | Local | Cloud | Notes |
|-----------|------|-------|-------|-------|
| `wan-2.2` | t2v  |   —   |   ✓   | FLAG-001: no local wan-2.2 t2v template; wan2.1 substitution forbidden |
| `wan-2.2` | i2v  |   ✓   |   ✓   | Local via wan22_i2v bare template (FLAG-003) |
| `wan-2.2` | flf  |   —   |   ✓   | Q1: fal image-to-video/turbo accepts `end_image_url` |
| `ltx-2.3` | t2v  |   ✓   |   ✓   | |
| `ltx-2.3` | i2v  |   ✓   |   ✓   | FLAG-005: local `image_ref` → `image` rename |
| `ltx-2.3` | flf  |   ✓   |   —   | A5: no cloud flf endpoint for ltx-2.3 |

**Absent cells** (not wired):

- **wan-2.2 t2v/local**: No local wan-2.2 text-to-video ready template exists,
  and wan2.1 substitution is forbidden by SD-001 (FLAG-001).
- **wan-2.2 flf/local**: No local wan-2.2 first-last-frame template exists.
- **ltx-2.3 flf/cloud**: The fal.ai LTX-2.3 API does not expose a
  first-last-frame endpoint (A5).

## Execution modes

### Local (`--execution local`)

Dispatches through `VibeComfyBackend` adapter (SD-004).  Uses
[vibecomfy](https://github.com/nosresearch/vibecomfy) to drive a local
ComfyUI backend.  The `vibecomfy` package is **lazy-imported** inside the
adapter — only loaded when `execution=local`.  Cloud-only invocations never
touch the package (SD-009).

Requires a running ComfyUI instance reachable from the executor process.

### Cloud (`--execution cloud`)

Dispatches through `FalBackend` adapter (SD-004).  Pure HTTP against
[fal.ai](https://fal.ai) using `astrid/core/util/http.py` `HttpClient`.
No fal SDK required (SD-009).

Requires `FAL_KEY` to be resolvable via the candidate-env-file walk
(see `astrid/core/util/secrets.py`).

## Escape hatch

**For frame-level control, multi-pass pipelines, keyframe conditioning, custom
samplers, LoRAs, or exotic video workflows, use `vibecomfy.run` directly.**
The `generation.generate_video` executor covers basic happy-path video generation
only (prompt, image_ref, image_end_ref, negative_prompt, seed, count,
resolution, frames, fps, duration, guidance_scale, steps).

See:
- `astrid/packs/vibecomfy/executors/run/STAGE.md` — VibeComfy workflow runner
- `docs/generation/31-video-contract.md` — video modality contract
- `docs/generation/` — modality contracts, manifest schema, feature list

## SDK quick-start

Use the public SDK; every executor invocation is attached to a project and
project-scoped runs write their outputs under that project's run tree.

```python
import astrid.sdk as sdk
result = sdk.invoke(
    "generation.generate_video",
    kind="executor",
    project="demo",
    inputs={
        "model": "ltx-2.3",
        "mode": "flf",
        "execution": "local",
        "prompt": "smooth transition",
        "image_ref": "frame0.png",
        "image_end_ref": "frameN.png",
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
    "generation.generate_video",
    kind="executor",
    project="demo",
    inputs={
        "model": 'wan-2.2',
        "mode": 't2v',
        "execution": 'cloud',
        "prompt": 'a serene mountain lake at dawn',
    },
    dry_run=True,
)
result = sdk.invoke(
    "generation.generate_video",
    kind="executor",
    project="demo",
    inputs={
        "model": 'wan-2.2',
        "mode": 'i2v',
        "execution": 'local',
        "prompt": 'animate this scene',
        "image_ref": './frame0.png',
    },
    dry_run=True,
)
result = sdk.invoke(
    "generation.generate_video",
    kind="executor",
    project="demo",
    inputs={
        "model": 'ltx-2.3',
        "mode": 'i2v',
        "execution": 'cloud',
        "prompt": 'animate this scene',
        "image_ref": './frame0.png',
    },
    dry_run=True,
)
result = sdk.invoke(
    "generation.generate_video",
    kind="executor",
    project="demo",
    inputs={
        "model": 'ltx-2.3',
        "mode": 'flf',
        "execution": 'local',
        "prompt": 'smooth transition',
        "image_ref": './frame0.png',
        "image_end_ref": './frameN.png',
    },
    dry_run=True,
)
result = sdk.invoke(
    "generation.generate_video",
    kind="executor",
    project="demo",
    inputs={
        "model": 'ltx-2.3',
        "mode": 't2v',
        "execution": 'local',
        "prompt": 'cyberpunk city flythrough',
        "resolution": '1280x720',
        "frames": 81,
        "fps": 24,
    },
    dry_run=True,
)
```

## Prompts file (JSONL)

For batch generation, use `--prompts-file` with a JSONL file (one JSON object
per line).  Each line may override `model`, `seed`, `count`, `resolution`,
`negative_prompt`, `image_ref`, `image_end_ref`, `frames`, `fps`, `duration`,
`guidance_scale`, and `steps`.  **Per-entry `model` overrides must include an
explicit `mode` field matching CLI `--mode`** (SD-005):

```jsonl
{"prompt": "a cat walking", "seed": 42, "frames": 81, "fps": 24}
{"prompt": "a dog running", "negative_prompt": "blurry", "resolution": "1280x720"}
{"prompt": "smooth transition", "model": "wan-2.2", "mode": "flf", "image_ref": "/path/to/start.png", "image_end_ref": "/path/to/end.png"}
```

`--prompt` and `--prompts-file` are mutually exclusive — providing both is
rejected at argparse.

## Validation rules

1. **Missing `--mode`** → rejected at argparse (required argument, SD-005).
2. **Missing `requires`** (e.g. `wan-2.2 --mode flf` without `--image-end-ref`)
   → hard-fail BEFORE any HTTP call or vibecomfy import.
3. **`--execution` must be `local` or `cloud`** — rejected at argparse.
4. **Mode `v2v` / `video-edit`** → rejected with "not wired this sprint".
5. **Unsupported features** (e.g. `--negative-prompt` on a cloud endpoint that
   doesn't accept it) → dropped with a `Warning` in the manifest; never
   hard-fail (SD-004).
6. **Per-entry mode mismatch**: If a prompts-file entry overrides `model`
   without a matching `mode` field, the entry is rejected.

## Output

- `{out}/videos/` — generated video files (e.g. `output_000.mp4`)
- `{out}/manifest.json` — canonical manifest conforming to
  `docs/generation/20-manifest-schema.md` (v2) with video extensions
  (per-output `duration_seconds`, `fps`, `resolution` via ffprobe best-effort)

## Golden demo

The mocked example `astrid/packs/generation/actions/generate_video/golden/demo_wan_ltx_local_cloud.py`
is available only in a source checkout and is not installed. Users should
invoke the declared `generation.generate_video` action normally.

## Design docs

- `docs/generation/00-features.md` — canonical feature list
- `docs/generation/10-registry-schema.md` — model registry schema (v2)
- `docs/generation/20-manifest-schema.md` — manifest JSON shape (v2)
- `docs/generation/31-video-contract.md` — video modality contract
- `astrid/packs/vibecomfy/executors/run/STAGE.md` — VibeComfy escape hatch
