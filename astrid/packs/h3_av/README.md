# H3 audiovisual transformations

This pack turns one small request into a deterministic preparation manifest,
an H3 workflow bundle, a generated candidate, and preservation evidence. The
public request uses `changes.video` and `changes.audio` schedules. The active
H3 pack contract supports source-backed edits, native continuation, reference-guided generation,
and explicit mask assets without creating a second project or task database.

## Character Swap (opt-in native-v2 candidate)

Native-v2 requests may opt into `settings.adapter.id: character_swap_v1`. The route
requires exactly one typed video reference and one typed replacement image, bound
to `<Video 1>` and `<Picture 1>`. Adapter strength defaults to `1.0` and must be
finite in `0..2`. It uses the immutable LoRA
`akatz-ai/MiniMax-H3-Character-Swap-LoRA`, revision
`62407e0cc8089c363abd9ce4b0b27662abb237af`, file
`h3_character_swap_pro4500_1000.safetensors`, SHA-256
`4b2a3f420ae804c0aa3422761ff84dbd1bf52eef6900ffab6d2e66df63cb4e79`.
The existing Ref2VA base, text encoder, VAEs, and optional Turbo LoRA retain
their current `resolve/main` asset declarations; only the Character Swap LoRA
has an immutable revision and checksum in this candidate.
The selected base remains Ref2VA (`minimax_h3_ref2va_pruned_int8_convrot.safetensors`);
an explicit different base is rejected rather than rewritten.

The model path is existing attention, Character Swap when selected, optional
Turbo when enabled, then existing sigma shift and sampler. Turbo defaults to
enabled at `0.95`; `settings.guidance` and `settings.guidance_scale` remain legacy
spellings for its strength and cannot be combined with `settings.turbo.strength`.
Disabling Turbo removes its loader and model requirement. The compilation records
effective settings and only connected model assets. We do not download LoRA
weights as part of request preparation or claim inference quality from this
candidate.

Source-free use needs no timeline media. Source-backed use accepts one bounded
video timeline at frame zero with explicit video edit permissions. The current
preparation probes the timeline container for an audio stream; the composer
preserves source audio where it exists and uses generated audio for uncovered
intervals. Explicit audio preservation requires a source audio stream. The
composer applies temporal full-frame video permissions and whole-stream audio
intervals. Regional
spatial masks, channel-specific audio edits, continuation, audio-only timelines,
and separate timeline audio or image anchors fail before execution. Runtime raw
publication and local candidate verification remain separate from final managed
publication; a passing compile or helper test does not establish that final
publication was accepted.

`operation: generate` accepts no source and one to nine ordered still-image
references, a prompt, output duration, and the normal settings overrides.
`shared/generation.py` builds one sampling graph from the existing H3 reference
node family. Images are simultaneous conditioning inputs, not successive
segments or implicit keyframes. `edit` and `continue` still require a source.

The compiler targets the packaged native Seitanism MiniMax H3 AV continuation
graph and LanPaint source-backed edit graph, plus the opt-in native-v2 Character
Swap graph. It fails closed when a request asks for a graph or composition
feature the selected route cannot represent.
Runtime admission and RunPod lifecycle remain Astrid/VibeComfy
responsibilities.

References are an ordered request list. The current continuation graph accepts
up to two image references; generation accepts one to nine; the edit graph
accepts none. Excess references fail admission or compilation. There is no
request-level `reference_strategy` or project-specific
workflow selection.

Compilation seals `capabilities.public_generation` into its digest. The
orchestrator derives `generation_intent` from that contract: all supported
routes declare one public video selector (`main-0`, ordinal 0).
The edit route's separate audio is a composition input, not another public
selector. Outputs and the composed candidate are retrieved through Runtime
managed media with size/hash verification before the final receipt is returned.

Generation requires empty video/audio change lists and creates the full AV
timeline. It supports 5..362 requested frames at 24 fps; native H3 lengths round
up to `5 + 17*k`. A 15-second request samples 362 frames, validates raw AV
coverage, and trims to exactly 360 frames and 15 seconds of audio. It does not
silently reduce duration or chain clips. For ordinary source-free generation,
video/audio references, timed keyframes, and source-free masks are not implemented
and fail closed. Character Swap is the separate native-v2 exception for one typed
video and one image reference. The default canvas is 1024×576; `guidance` uses
the existing Turbo LoRA strength convention.

The generated bundle is self-contained and portable. Reference filenames are
managed run inputs; media defaults remain empty to preserve VibeComfy's
emission/reload semantic identity. Submit through `h3_av.transform` with
`kind="orchestrator"`; do not hand-submit a separate VibeComfy generation.
CPU tests cover structure, bindings, duration and managed transport. Live target
schema/model attestation, GPU memory sufficiency, and visual acceptance remain
to be tested on the target machine.
