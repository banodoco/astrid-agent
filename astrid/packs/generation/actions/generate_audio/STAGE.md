# Generate Audio

**Executor**: `generation.generate_audio`  
**Modality**: audio (`schema_version: 2`)  
**Status**: implemented (cloud-first; local ComfyUI audio is a follow-up)

Generates audio from text prompts (or prompt files) using local (vibecomfy)
or cloud (fal) backends.  A single executor dispatches through `BackendAdapter`
(SD-004) — callers pick a model, a mode, and a backend; the executor does the
rest.  **`--mode` is required** (SD-005).

The direct executor backend loop is `music`. The typed SDK facade accepts
`mode="tts"` and routes it to `generation.generate_speech`, which emits exact
speech plus WordBoundary timing; direct action invocation of `tts` remains a
clear validation error. `sfx` remains reserved for a future action.

## Starting models (cloud)

| Model                  | Mode  | Local | Cloud endpoint                                      | Notes |
|------------------------|-------|-------|-----------------------------------------------------|-------|
| `stable-audio-3-medium`| music | —     | `fal-ai/stable-audio-3/medium/base/text-to-audio`   | Long-form stereo music; supports `negative_prompt`, `duration`, `guidance_scale`, `steps`, `output_format`. |
| `minimax-music-v2.6`   | music | —     | `fal-ai/minimax-music/v2.6`                         | Prompt + lyrics; set `instrumental=true` to skip lyrics. |
| `minimax-music-3`      | music | —     | `minimax/music-3`                                   | Current-gen song model (up to 5 min); **lyrics required**, structured caption + section tags; priced per second. |
| `minimax-music-3.0`    | music | wavespeed | `https://api.wavespeed.ai/api/v3/minimax/music-3.0` | MiniMax Music 3.0 via WaveSpeedAI; full songs, structure tags, no duration/seed knobs; `$0.15`/song. Requires `WAVESPEED_API_KEY`. |
| `ace-step`             | music | —     | `fal-ai/ace-step/prompt-to-audio`                   | Style prompt → tags/lyrics; priced per second of duration. |

All entries are registered in `astrid/core/model_catalog/models.yaml` under
`schema_version: 2`.  Each entry declares per-mode `supports: [...]`,
`requires: [...]`, and per-backend `param_map` entries (SD-003).

## Execution modes

### Local (`--execution local`)

Not wired this sprint.  The escape hatch for local ComfyUI audio pipelines is
`vibecomfy.run` (see below).  A follow-up sprint will add vibecomfy
ready-templates for Stable Audio 3, ACE-Step, and Stable Audio Open, plus the
necessary `VibeComfyBackend` node-target wiring.

### Cloud (`--execution cloud`)

Dispatches through `FalBackend` adapter (SD-004).  Pure HTTP against
[fal.ai](https://fal.ai) using `astrid/core/util/http.py` `HttpClient`.
No fal SDK required (SD-009).

Requires `FAL_KEY` to be resolvable via the candidate-env-file walk
(see `astrid/core/util/secrets.py`).

## Escape hatch

**For spectrogram conditioning, multi-track generation, vocal inpainting, custom
audio samplers, and exotic audio workflows, use `vibecomfy.run` directly.**
The `generation.generate_audio` executor covers the basic happy-path only
(prompt, negative_prompt, seed, count, duration, guidance_scale, steps,
lyrics_prompt, instrumental, output_format).

See:
- `astrid/packs/vibecomfy/executors/run/STAGE.md` — VibeComfy workflow runner
- `docs/generation/32-audio-contract.md` — audio modality contract
- `docs/generation/33-music-models.md` — cloud model and local workflow reference
- `docs/generation/` — modality contracts, manifest schema, feature list

## SDK quick-start

```python
import astrid.sdk as sdk
result = sdk.invoke(
    "generation.generate_audio",
    kind="executor",
    project="demo",
    inputs={
        "model": "stable-audio-3-medium",
        "mode": "music",
        "execution": "cloud",
        "prompt": "a serene ambient drone",
    },
    dry_run=True,
)
```

Project-scoped runs write outputs inside the project's run tree; do not pass
`out` together with `project`.

## Equivalent SDK calls

Direct module invocation is rejected by the canonical-entrypoint guard; use
the public SDK action path shown here and in the SDK quick-start above.

```python
import astrid.sdk as sdk
result = sdk.invoke(
    "generation.generate_audio",
    kind="executor",
    project="demo",
    inputs={
        "model": 'stable-audio-3-medium',
        "mode": 'music',
        "execution": 'cloud',
        "prompt": 'a serene ambient drone',
    },
    dry_run=True,
)
result = sdk.invoke(
    "generation.generate_audio",
    kind="executor",
    project="demo",
    inputs={
        "model": 'minimax-music-v2.6',
        "mode": 'music',
        "execution": 'cloud',
        "prompt": 'upbeat synth-pop chorus',
        "lyrics_prompt": 'We are the robots, beep boop',
    },
    dry_run=True,
)
result = sdk.invoke(
    "generation.generate_audio",
    kind="executor",
    project="demo",
    inputs={
        "model": 'minimax-music-v2.6',
        "mode": 'music',
        "execution": 'cloud',
        "prompt": 'cinematic orchestral score',
        "instrumental": 'true',
    },
    dry_run=True,
)
result = sdk.invoke(
    "generation.generate_audio",
    kind="executor",
    project="demo",
    inputs={
        "model": 'ace-step',
        "mode": 'music',
        "execution": 'cloud',
        "prompt": 'lo-fi hip hop beat',
        "duration": 30,
        "steps": 50,
    },
    dry_run=True,
)
result = sdk.invoke(
    "generation.generate_audio",
    kind="executor",
    project="demo",
    inputs={
        "model": 'minimax-music-3',
        "mode": 'music',
        "execution": 'cloud',
        "prompt": 'Genre: ethereal synth-pop. BPM: 112.',
        "lyrics_prompt": '[verse]\\nSoftly the world begins to breathe\\n[chorus]\\nSing it again',
        "duration": 180,
    },
    dry_run=True,
)
```

## Prompts file (JSONL)

For batch generation, use `--prompts-file` with a JSONL file (one JSON object
per line).  Each line may override `model`, `seed`, `count`, `duration`,
`negative_prompt`, `guidance_scale`, `steps`, `lyrics_prompt`, `instrumental`,
and `output_format`.  **Per-entry `model` overrides must include an explicit
`mode` field matching CLI `--mode`** (SD-005):

```jsonl
{"prompt": "a serene ambient drone", "seed": 42, "duration": 30}
{"prompt": "upbeat synth-pop chorus", "lyrics_prompt": "We are the robots", "duration": 60}
{"prompt": "cinematic orchestral score", "model": "minimax-music-v2.6", "mode": "music", "instrumental": true}
```

`--prompt` and `--prompts-file` are mutually exclusive — providing both is
rejected at argparse.

## Validation rules

1. **Missing `--mode`** → rejected at argparse (required argument, SD-005).
2. **Mode `tts` / `sfx`** → rejected with "not wired this sprint".
3. **Missing `requires`** (only `prompt` for the music models above) → hard-fail
   BEFORE any HTTP call.
4. **`--execution` must name a registered backend** such as `local` or `cloud`.
5. **Unsupported features** (e.g. `--lyrics-prompt` on `stable-audio-3-medium`) →
   dropped with a `Warning` in the manifest; never hard-fail (SD-004).
6. **Per-entry mode mismatch**: If a prompts-file entry overrides `model`
   without a matching `mode` field, the entry is rejected.

## Output

- `{out}/audio/` — generated audio files (e.g. `output_000.mp3`)
- `{out}/manifest.json` — canonical manifest conforming to
  `docs/generation/20-manifest-schema.md` (v2) with audio extensions
  (per-output `duration_seconds` via ffprobe best-effort)

## Design docs

- `docs/generation/00-features.md` — canonical feature list
- `docs/generation/10-registry-schema.md` — model registry schema (v2)
- `docs/generation/20-manifest-schema.md` — manifest JSON shape (v2)
- `docs/generation/32-audio-contract.md` — audio modality contract
- `docs/generation/33-music-models.md` — music model reference
