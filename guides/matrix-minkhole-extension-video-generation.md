# Matrix — Into the Minkhole: H3 extension workflow

Updated 23 September 2026. Project: `matrix-minkhole`. Parent timeline:
`rough-cut`.

This is the project-specific companion to [Anchor-to-anchor video
generation](anchor-to-anchor-video-generation.md). It describes how to use a
short piece of the original Morpheus footage as an audiovisual start state and
extend it into new motion. It is a preparation and review procedure, not proof
that a final generation has been accepted.

## Decide between extension and inpainting

Use the two techniques for different continuity problems:

| Need | Use | Why |
| --- | --- | --- |
| The body, hands, camera, and pose must remain exactly the same while the mouth/voice or a local object changes | [Replacing speech in an existing video](replacing-speech-in-existing-video.md) and its LanPaint H3 AV inpainting candidate | A tracked mask protects the rest of the source frame and retains unmasked audiovisual context. |
| A short real clip can establish the speaker, then the shot may move, gesture, or continue beyond the source | This guide and the pinned Seitanism extension workflow | The native H3 latent edge carries motion/audio context into newly generated frames. |
| A generated section must land on a known destination pose or image | The extension workflow plus the end-frame variant under `workflows/seitanism_h3_av_extension_end_frame/` | An endpoint guide is an additional constraint; it is not a substitute for checking the motion path. |

Extension is not a guaranteed voice-cloning or exact-dialogue-replacement
method. The verified graph keeps the source audio in its current controller
configuration and generates continuation material after the source prefix. If
the existing words must change in place, use the inpainting path. If the new
words must be exact, treat generated audio as an experiment and keep final
dialogue on a separately verified audio/lip-sync path.

## Source media and project inventory

The original managed source is:

```text
project:    matrix-minkhole
media:      matrix-red-blue-pill-edit.mp4
object:     sha256:059624abaab9d27a3d9901e0c78961cc10c995890ecd200f254a9cbb1612cba7
format:     1920x1080, 23.976 fps, 122.044 s, AAC stereo 44.1 kHz
```

The media identity comes from Astrid's runtime, not from a checkout copy. The
current `rough-cut` is parent version 33. Its six picture shots are assembled
from child timelines, and source time must not be confused with edited time or
the temporary voiceover timing.

### Speaking start clips found in the source

The following ranges are the useful starting candidates after inspecting the
actual source frames:

| Purpose | Child timeline / clip | Source interval | What it provides |
| --- | --- | ---: | --- |
| Primary extension pilot | `shot-5e2f57436eb8fa88` / `shot-5e2f57436eb8fa88-picture-2` | `81.05–84.30s` | Morpheus in a stable close-up, visibly speaking, with the red chair and dark room lighting held consistently. This is the best 3.25-second start clip for testing continuation. |
| Short “two options” start | `shot-f7a610977b95d2e9` / `choice-picture-1` | `74.9347368421–76.40s` | Morpheus close/medium framing with both hands near his chest. The edited shot slows this source interval to three seconds, so use the original source timing when staging H3. |
| Context only, not a speaking start | `shot-5e2f57436eb8fa88` / `shot-5e2f57436eb8fa88-picture-0` | `79.12–80.87s` | Blue-pill hand/object action. Useful as visual context, but it does not give H3 a speaking face. |
| Reaction only | `shot-5e2f57436eb8fa88` / `shot-5e2f57436eb8fa88-picture-1` | `76.55–78.95s` | Neo reaction. Do not use it as the speaker start clip. |
| Reaction only | `shot-5e2f57436eb8fa88` / `shot-5e2f57436eb8fa88-picture-3` | `93.50–95.7944954128s` | Neo reaction. Do not use it as the speaker start clip. |

The red-pill, creature-reveal, reflection, and continuation child timelines are
currently still/image or compositor-led sections. They do not yet contain a
verified live speaking start clip. Do not pretend that a still is a successful
extension input; either stage an independent H3 start from an approved keyframe
or first choose a real source speaking passage.

### Dialogue bridge used for the current pilot

The source-video bridge begins with Morpheus saying **“This is your last
chance.”** That line must remain in the H3 continuation prompt as the first
spoken beat; it is not an optional placeholder or a line to infer later. The
current experimental follow-on target is **“You can poo or pee on my face.”**
Use the exact two-line target in the prompt for this pilot:

```text
Begin with the exact spoken line: “This is your last chance.” Then continue with the exact spoken line: “You can poo or pee on my face.”
```

Keep the original speaking audio in the supplied audiovisual prefix. Because the
extension graph is not a guaranteed exact-dialogue or voice-cloning method,
review the generated words and lip sync separately before accepting the take;
use the inpainting workflow when the existing picture and pose must remain
fixed.

For the first pilot, use `81.05–84.30s` without the rough-cut speed change.
Preserve a small amount of neighboring context when extracting it, but record
the exact H3 input trim separately from the editorial shot time. The source
contains the original Movieclips watermark; confirm that the licensed/clean
source intended for the final pass is available before treating this as final
footage.

## Canonical workflow

Use the complete canonical bundle:

- Python: [`workflows/seitanism_h3_av_extension_repaired/workflow.py`](../workflows/seitanism_h3_av_extension_repaired/workflow.py)
- UI/source graph: [`workflows/seitanism_h3_av_extension_repaired/source.json`](../workflows/seitanism_h3_av_extension_repaired/source.json)
- VibeComfy companion: [`workflows/seitanism_h3_av_extension_repaired/workflow.vibe.json`](../workflows/seitanism_h3_av_extension_repaired/workflow.vibe.json)
- Import provenance: [`workflows/seitanism_h3_av_extension/PROVENANCE.md`](../workflows/seitanism_h3_av_extension/PROVENANCE.md)

The source bundle is pinned to the Seitanism H3 Motion Context/MultiRef
workflow imported from commit `361624fb406b63eb6694442eac6c895fc1533a70`.
The graph uses the H3 reference-to-video conditioning, a starter sampler,
then six sequential extension samplers. Every generated continuation passes
through `MiniMaxH3GeneratedAVMaskedContext`; the final writer is
`MiniMaxH3StreamLiveExtensionAVToVHS`.

Important graph contracts:

- input video is normalized through `VHS_LoadVideoFFmpeg` at 24 fps and checked
  by `MiniMaxH3Validate24FPSVideo`;
- the extension chain retains a **39-native-frame audiovisual context** and
  uses a 39-frame overlap at the output boundary;
- the graph has six chained extension slots, with 8 effective sampler steps and
  the pinned FL2V turbo LoRA at 0.95;
- the source-audio controller is `Keep source audio`; this is why this graph
  is a continuation experiment, not the exact in-place dialogue replacement
  route;
- the output is `video/masked_av_extension` as an H.264 MP4 candidate;
- the workflow's model/runtime requirements are H3 Ref2VA, the Qwen3-VL text
  encoder, H3 video/audio VAEs, ComfyUI `0.36.0`, Torch `2.10.0+cu130`,
  `comfy-kitchen 0.2.34`, and `comfy-aimdo 0.5.3`, with
  `--use-ck-attention --disable-comfy-compiler`.

### H3 source audiovisual preparation contract

“24 fps with original audio” is not sufficient preparation for this graph. H3
needs one concrete audiovisual prefix whose decoded frame and audio timelines
agree. Choose an integer frame count on the 24-fps grid, decode/re-encode from
one source boundary with zero-based timestamps, and record the actual frame
count, audio sample rate, channels, decoded sample count, loader settings and
source digest. Prefer a PCM-audio MOV when codec delay or AAC priming would be
ambiguous; the final decoded artifact still has to be checked.

For an H3 source with `N` loaded frames, the expected audio boundary is
`round(N / 24 * sample_rate)`. The preparation target is exact agreement. The
worker allows only a small 0.5% decoder/container correction; do not increase
that tolerance, stretch speech to hide a larger mismatch, or rely on H3's
silence fallback for a speaking-prefix task. Re-extract the source window when
the check fails.

The shared Astrid invocation preflight performs this CPU-only check before
`tasks create`, after the exact canonical bundle and managed asset digests are
known. The worker repeats it after staging, before model warm-up or Comfy queue
submission. It also checks the compiled `source_video` basename, the active
workflow prompt, and reachable nonblank media inputs. Structural
`vibecomfy validate --no-schema` remains necessary, but it cannot replace this
invocation/media check.

Remote execution has one additional contract beyond generation readiness. The
RunPod checkout session's manager-owned Comfy output directory must be present
in its readiness binding. The worker passes that exact path into VibeComfy,
rejects conflicting workflow or environment output roots, verifies the actual
declared MP4 after Comfy finishes, and downloads a hash-matched private copy
before the Astrid task can settle. A successful Comfy prompt or a file visible
on the pod is not yet a successful Astrid generation.

The first Morpheus pilot exposed why this gate exists. The original short
`74.50–76.40s` MP4 loaded as 47 frames but supplied only 60,930 decoded stereo
samples at 32 kHz; H3 required 62,667, a 2.772% mismatch, so the worker rejected
it before sampling. The prepared replacement is
`morpheus-speaking-74p50-h3-aligned.mov`, managed as
`sha256:08761be22abb5d603ec6cf205ee86f1aec8de32f7c6229805c9f8596f988a91a`.
It loads as 47 frames and decodes to 62,650 samples, within the intentional
correction bound, while retaining the original speaking audio.

The end-frame and reference-only variants are useful comparison candidates,
but do not silently substitute either into the first pilot:

- [`seitanism_h3_av_extension_end_frame`](../workflows/seitanism_h3_av_extension_end_frame/README.md)
  injects a destination image at a target frame;
- [`seitanism_h3_av_extension_end_frame_reference`](../workflows/seitanism_h3_av_extension_end_frame_reference/README.md)
  uses a second image as appearance guidance without a target-frame insert.

The upstream node author's keyframe notes are also relevant:
[KEYFRAMES_AND_INSERTS.md](https://github.com/seitanism/ComfyUI-H3-Motion-Context-MultiRef/blob/main/KEYFRAMES_AND_INSERTS.md).

## RunPod setup and handoff

> `scripts/claim_runpod_5090_backup.py` is an operator tool, not the generation path; generation is `astrid generate video --entry ... --placement runpod`.

The long-running claimant is the repo script
[`scripts/claim_runpod_5090_backup.py`](../scripts/claim_runpod_5090_backup.py).
Run it from the Astrid checkout with the repo virtualenv; the system Python may
have the `runpod-lifecycle` executable but not the importable library:

```bash
.venv/bin/python scripts/claim_runpod_5090_backup.py \
  --gpu-type "NVIDIA GeForce RTX 5090" \
  --storage-name backup \
  --container-disk-gb 200 \
  --image "runpod/base:1.0.3-dev-fix-pytorch-version-verification-cuda1300-ubuntu2404" \
  --allowed-cuda-versions 13.0 \
  --handle-path .runpod-jobs/matrix-minkhole-5090-pod-handle-20260923.json
```

As of this guide update, the waiter is active but RunPod has reported no
available RTX 5090 instance matching the `backup` volume in its current
capacity windows. That is a provider-capacity condition, not a passed claim.
The script must not report success until SSH is ready and the mounted release
passes the real CUDA-13 Torch probe. A successful handle is secret-free and
leaves the pod running.

After a handle exists, verify the exact pod and volume, then use the practical
standalone lifecycle path for the first job:

```bash
runpod-lifecycle list --json
runpod-lifecycle run <pod-id> \
  --script /absolute/path/to/matrix-minkhole-extension/run.sh \
  --remote-root /workspace/jobs/matrix-minkhole-extension-<stamp> \
  --upload-mode sftp_walk \
  --timeout 900 \
  --keep-pod \
  --json
```

The current `workflows/astrid_intro_h3_anchor_chain/run_pod.sh` is an
Astrid-Intro-specific runner with hard-coded `/workspace/astrid_h3` paths. It
is a reference for the remote VibeComfy invocation, not a Matrix job script to
run unchanged. Stage the Matrix source trim, 24-fps input, source-tail image,
optional reference/end-frame image, canonical workflow bundle, and a fresh
job script under a dedicated directory. Do not put those files beside
credentials, model caches, or prior artifacts.

Before spending on a creative take, run the lifecycle guide's H3 capacity
smoke: one 1920x1088, 260-native-frame, one-step diagnostic through the real
graph. Only after that passes should the 81.05–84.30s Morpheus pilot be queued.
The transport result, SSH/GPU presence, or a successful VibeComfy compile alone
does not establish H3 generation readiness.

## First pilot procedure

1. Read the current Astrid `rough-cut` and child timeline versions again. Keep
   the managed source digest and exact source trim in the attempt record.
2. Extract the source speaking interval at its native source timing, normalize
   it to the workflow's 24-fps input contract, and retain the original audio.
   Do not use the slow-motion editorial clip as the H3 input.
3. Use the speaking clip as the existing-video starter. Use the last clean
   source frame as the starter visual reference only when the graph input
   contract requires it; do not use a rendered screenshot with Astrid labels or
   overlays.
4. Prompt only the continuation beats: preserve Morpheus's identity, chair,
   lighting, camera position, and the incoming mouth/pose; then describe the
   next physical action. Do not ask the graph to restart the scene or to match a
   still's pose after the protected prefix.
5. Keep the H3-generated audio out of the final Astrid timeline until the
   voice, words, room tone, and boundary have been explicitly reviewed. The
   current Minkhole edit's temporary Daniel voiceover remains a separate track.
6. Collect the raw MP4 and native latent continuation evidence. Never decode an
   MP4 and feed it back as the next H3 context; descendants must receive the
   direct 39-frame audiovisual latent edge.
7. Review the raw take in motion, then inspect the final preserved frame, first
   unprotected frame, first 10–20 generated frames, endpoint, and outgoing cut.
   Reject wrong motion, identity drift, invented camera moves, lighting changes,
   or audio discontinuities at the generation layer.
8. Only after the pilot passes should the method be repeated for another
   speaker passage. Each segment gets its own source trim, prompt, seed,
   workflow/runtime hashes, output digest, technical verdict, and approval
   status.

## Assembly and acceptance

Generated video is a candidate until it is imported through Astrid, placed on
the owning child timeline, and reviewed in the parent composition. Keep the
original still/source clip and every unselected take reversible. Do not flatten
the project into a replacement movie.

Use the review loop from [Replacing speech in an existing video](replacing-speech-in-existing-video.md):
render with `--review`, inspect filmstrip and cuts, check waveform/audio around
the join, and save only through the version-checked Astrid timeline path. Use
the full anchor-to-anchor checklist for raw motion, endpoint, overlap, context
removal, and descendant provenance. A decodable MP4 or a successful remote exit
is not visual or editorial acceptance.

## Readiness checks

The workflow must pass both structural and invocation checks. First validate
the complete canonical bundle without a live schema:

```bash
vibecomfy validate workflows/seitanism_h3_av_extension_verified \
  --no-schema --yes --json
```

The normal local schema-backed inspect/validate path currently reports missing
captures for `BasicGuider`, `ModelAttentionBackend`, `PreviewAny`, and
`ResolutionSelector`. That is a local schema/readiness limitation; do not edit
the canonical graph to remove those nodes. Resolve it against the actual
RunPod ComfyUI `/object_info` before claiming execution readiness, then rerun
`vibecomfy inspect`, `vibecomfy validate`, and `vibecomfy doctor` against that
runtime. Then submit only through the canonical Astrid task path with the
complete Python/companion/source trio and filename-bearing `source_video`
descriptor. The SDK and worker invocation preflights must both report the
exact bundle/member digests, compiled source binding, approved end-state
prompt, reachable media set, and H3 frame/sample measurement before a creative
run is considered ready.
