# One h3 video-edit live input

Run from the checkout/install root containing `live-inputs/` and `inputs/`.
On the live controller this is `/home/user/wt/install-r13b`.
`asset-map.json` resolves paths relative to its own directory.

Place the real still at `inputs/edit-src.png` (1280x720 PNG) and a matching
source video at `inputs/h3-video-edit-source.mp4`. The still is the ordered
`<Picture 1>` reference, bound as `reference_0`; it is guidance, not a fixed
first-frame constraint. The timeline source binds as `source_video`.

Create a compatible 2-second source from that still (or conform real footage):

```sh
ffmpeg -hide_banner -loglevel error -loop 1 -framerate 24 -i inputs/edit-src.png -f lavfi -i anullsrc=r=44100:cl=stereo -vf 'scale=1024:576,setsar=1' -t 2 -r 24 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest inputs/h3-video-edit-source.mp4
```

This produces 48 frames, exact 24/1 CFR, 1024x576, H.264 MP4 with silent AAC.
Only the first six frames are selected. Real footage must cover that window;
use the same rate/canvas and an audio stream for this audio-preserving request.
Preflight enforces rate/canvas and audio/video duration agreement within 1/24 s
in `astrid/packs/h3_av/shared/prepare.py:188-249`; it does not mandate H.264.
The graph loads source audio, so this recipe explicitly supplies AAC silence.

CPU proof (the public CLI uses declared inputs, not --request/--asset-map):

```sh
astrid generate video --entry h3 --mode video-edit --input request=live-inputs/h3-video-edit/request.json --input asset_map=live-inputs/h3-video-edit/asset-map.json --placement runpod --dry-run --out /home/user/wt/h3edit.env/proof
```

Live command, for the controller's already claimed machine (not run here):

```sh
astrid generate video --entry h3 --mode video-edit --input request=live-inputs/h3-video-edit/request.json --input asset_map=live-inputs/h3-video-edit/asset-map.json --placement runpod --consent user_env --out outputs/h3-video-edit-live
```

Use a fresh output directory. This head has no --machine flag; --placement
runpod requires an existing live lease in the same controller's custody journal
(`astrid/sdk/generation.py:290-309`). Generation does not claim a new pod.
`scripts/live_cells.json` lists five cells without machine flags; Release 13's
older paid-generation examples predate this head's claim/generate separation.

Expected final candidate: six frames, 0.25 s, 24 fps, 1024x576, composed MKV
with source audio preserved (lossless FFV1 video and PCM audio for this input).
Raw graph output is 22 frames / 0.916666667 s, H.264 MP4 with audio; composition
removes 16 tail frames. See `shared/generation.py:346,430-433`,
`shared/compile.py:501-505,550-567,599-606`, and `shared/compose.py:979-985,1305-1318`
under `astrid/packs/h3_av/`. Steps=1 is the accepted minimum
(`schemas/request.v2.json`, settings.steps). Runtime and peak VRAM are unknown;
one step is a transport smoke setting with no visual-quality claim.

The timing helper permits 5..362 frames and raw lengths 5+17*k
(`shared/generation.py:195-202`, `shared/timing.py:49-54`). Five frames requested
as exact "5/24" failed the real CLI: `ops[0].during[1] is not representable on
the native clock`. V2 normalization emits a float (`shared/request_v2.py:62-63`),
then canonical spec rechecks it exactly (`shared/spec.py:218`,
`shared/request_v2.py:70-84`). Six frames / 0.25 s is the shortest representable
accepted window at or above five on this path; the delivered request uses it.

CPU proof on Agent 65b1d45c6a33b7b6fab0b3f8df3aa3aba5f8963e,
Runtime 6855dfa8df65cf8ae4c002b57134d6505493c17c,
VibeComfy 46251c8cc5565e555356e74e3179223a40f978f7:
exit 0, status=ready, issues=[], fixes=[], accepted_inputs=[asset_map,request],
rejected_inputs=[], unbound=[], source frame cap=6, steps=1,
source_video=9cdb3605decc5823-6b978229d169830e.mp4,
reference_0=1a095337fa361926-3d7fa1d53c5434db.png,
compilation_digest=68782b8b39c98821b27c3bf71ed3729089d24064a651819c479220f237550b7b.
These asset bindings/digest describe the Pillow/ffmpeg CPU stand-ins; real media
will produce different hashes. Defaults resolved all three video modes to h3
at priority 100. Describe listed only request and asset_map; although it labels
them optional, the build requires both.

The dry run judged declarations, resolved files, and compiled on CPU. Its target
state is pending_host_readiness; it emits no live-target approval. GPU execution,
asset shipment, target schemas, composition and managed retrieval remain live
checks. No tests, model downloads, paid calls or inference ran here.
The five locked weight files, two pinned node packs, CUDA-13/Torch runtime and
attention packages are declared in `shared/generation.py:239-283` and the
RunPod model lock. The weights alone total 53,484,234,760 bytes; this is disk
size, not VRAM. The controller needs its existing Runtime authorization and
RunPod credential configuration; no additional inference API key is declared.
