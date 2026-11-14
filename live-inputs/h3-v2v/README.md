# One h3 v2v live input

Base: Agent `65b1d45c6a33b7b6fab0b3f8df3aa3aba5f8963e`, VibeComfy `46251c8cc5565e555356e74e3179223a40f978f7`.

Place these JSON files under `live-inputs/h3-v2v/` in the live checkout.
The asset map resolves paths relative to itself. In `/home/user/wt/install-r13b`, provide:

- `inputs/edit-src.png`: the existing 1280x720 still, bound as `reference_0` / `<Picture 1>` (guidance, without a hard first-frame constraint).
- `inputs/h3-v2v-source.mp4`: a real 2-second video, 48 frames, exactly 24/1 CFR, 1024x576, H.264/yuv420p with matching-duration AAC audio. Silent AAC is sufficient. Only its first six frames are selected.

The source rate and dimensions are enforced in `astrid/packs/h3_av/shared/prepare.py:238`; codec is a compatible preparation choice, not a video-codec restriction enforced there. Audio/video duration drift must be at most 1/24 second. For an audio-free input, this creates a compatible source with silent audio:

```sh
ffmpeg -i REAL_VIDEO -f lavfi -i anullsrc=r=44100:cl=stereo -map 0:v:0 -map 1:a:0 -t 2 -vf 'fps=24,scale=1024:576,setsar=1' -c:v libx264 -pix_fmt yuv420p -crf 19 -c:a aac -shortest inputs/h3-v2v-source.mp4
```

Run from the live checkout root, in the same installed Astrid workspace that owns the existing claim:

```sh
astrid generate video --entry h3 --mode v2v --input request=live-inputs/h3-v2v/request.json --input asset_map=live-inputs/h3-v2v/asset-map.json --placement runpod --dry-run
```

Live argv (prepared only; not executed by this worker):

```sh
astrid generate video --entry h3 --mode v2v --input request=live-inputs/h3-v2v/request.json --input asset_map=live-inputs/h3-v2v/asset-map.json --placement runpod --out outputs/h3-v2v
```

This head has no `--request`, `--asset-map` or generation `--machine` flag. M2 separates claim from generation; the live command needs the already claimed machine in the same workspace. `scripts/live_cells.json` declares the other five entry/mode pairs, without placement flags. No paid-consent or claim-budget flags belong on this generate command. Do not run another claim for this job.

The job regenerates full-frame video over [0, 0.25) and preserves source audio. Expected delivery: one 6-frame, 0.25-second, 24-fps, 1024x576 composed MKV (lossless H.264 and PCM audio). The internal graph emits a 22-frame H.264 MP4; composition trims the 16-frame tail. See `shared/generation.py:195`, `shared/timing.py:49`, and `shared/compose.py:1305` under the h3_av pack.

One step is the public minimum (`shared/request_v2.py:219`); the default is eight. This is a minimum-cost execution probe, with no quality claim. The canvas and fps are fixed by the graph (`shared/generation.py:346` and `:352`). Five frames is the graph minimum, but even exact `"5/24"` timing fails source-backed canonicalization: `shared/spec.py:563` and `:218` revalidate normalized float intervals through `shared/request_v2.py:70`. Six frames is the shortest decimal-exact duration above that minimum.

CPU proof uses the installed CLI from `h3v2v.env/venv/bin/astrid`, with the setup script's pinned dependencies, scrubbed environment and disposable local Runtime workspace. Stand-ins: Pillow RGB 1280x720 PNG; ffmpeg looped it into a 2-second 1024x576/24-fps H.264 + silent AAC MP4. No tests, GPU inference, provider calls, weights or model API calls. Stand-in bytes are not committed. Live source bytes will change the manifest digests.

Runtime and VRAM: unknown. The five H3 weight files total 53,484,234,760 bytes on disk; that is not a VRAM estimate. The live worker needs the recipe's CUDA-13 ComfyUI environment, both pinned H3 Motion Context and VideoHelperSuite node packs, the five locked H3 weights, ffmpeg/ffprobe, Runtime/host access, and its existing provider credential (normally `RUNPOD_API_KEY`; `HF_TOKEN` only if its download setup requires it). Dry run cannot prove their presence, target-schema approval, GPU compatibility, composition success, editorial quality or completion within the claim deadline.

CPU dry run: PASS, exit 0, `status: ready`, `issues: []`, `unbound: []`, accepted inputs `[asset_map, request]`; target `pending_host_readiness`. Resolved bindings: `source_video=31a6a061ae469d2a-6b978229d169830e.mp4`, `reference_0=98d2c43f2ce6e9e9-62a4f85394cd103e.png`, `mask_0=60f587d2d6d543ea-resolved-mask.png`. Compilation digest: `1d031c02bb1fe7d90a3d7548a774e0cad4396e72884eadfa0858c4d625eae7bd`. This CLI emits no target `approval` field for this dry run; its ready declaration plan is the CPU proof.
