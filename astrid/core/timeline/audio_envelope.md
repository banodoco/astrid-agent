# Authored and compiled audio gain

`audio_envelope.py` validates deliberately authored `clip.audio.envelope` and
compiles transient render projections using the same gain representation. The
strict document schema admits only `{audio: {envelope: ...}}`; renderer-owned
`audio.render_source` and `compiled_audio` carriers never persist. Each envelope has ordered `keyframes: [{at, value, curve?}]`; a point's
curve controls its following segment, defaulting to `linear`. Times are seconds
relative to the compiled clip's timeline start. Outside the keys, endpoint gains
hold. Falling segments traverse the registry curve backwards, so equal-power
pairs preserve uncorrelated power. `multiply` composes other envelopes;
`minimum` takes the strongest of overlapping attenuations before multiplication.
All leaves use this same keyframe primitive, and all curve definitions come from
`audio_curves.json`. No expression or executable author data is accepted.

`compile_legacy(clip, fps)` projects rounded legacy fade windows, including the
product when fades overlap. Existing authored `audio.envelope` takes precedence.
The equivalent Composition adapter is `compileAudioEnvelope`; the generated
registry is synchronized with `scripts/gen_audio_curves.py --composition-root`
and checked with `--check`. Base clip/track volume and mute remain independent.

`astrid.sdk.timeline.audio_envelope.compile_clip_audio(clip)` consumes existing
`tag(duck=...)` intent and current `tl.words()` after timing compilation. It
returns the transient `audio` value. Attack ends at speech start and release
starts at speech end; frame durations use the checkout fps. Words retain the
existing narration timing semantics, including reflow/splits. Project every
resulting segment from its own `clip.start`, using the same resolved words.
This adapter does not write a document, persist words, or arrange music.

Composition Player decodes source PCM, applies gain per sample in timeline
coordinates, then plays a float WAV with the existing media trim, playback rate
and base volume. Its buffering handle holds playback during decoding; URLs are
revoked on revision/unmount. This requires browser Web Audio and fetchable media.
External Remotion encoding receives an invocation-served float WAV prepared by
`astrid.core.rendering.envelope_pcm`. Preparation multiplies source samples by
the same compiled gain plan at `(source_time - rounded_trim) / playback_rate`.
It preserves the full source clock: AudioTrack applies source trim, playback rate
and base volume once. Its transient `audio.render_source` marker binds the PCM
URL to the envelope and sample clock; the hook refuses missing or mismatched
preparation rather than baking a browser blob for an external encoder. Each
occurrence has its own file in the existing disposable asset-materializer stage.
FFmpeg evaluates the shared plan directly with `aeval`, then places clips at
sample offsets and mixes without normalization. Unknown curves and unavailable
sample consumers refuse with `ENVELOPE_UNRENDERED`; missing narration or duck
consumers refuse with `DUCK_UNRENDERED`.

Managed publication and frozen candidate preview share `ManagedRenderSnapshot`'s
final audio compilation after occurrence/timing projection and before admission.
The producer consumes the admitted projected audio clips' word rows, never a live
checkout. `RenderRequest.compiled_audio` carries schema version 1, source-context,
timeline-config and curve-registry digests, plus projected clip IDs mapped to the
existing `{envelope: ...}` values. The admitted authority also pins the payload
checksum. Public SDK inputs cannot supply this host-owned carrier or authority.

When duck declarations overlap on the same projected occurrence, track and
canonical `asset` selector, the snapshot compiler takes their pointwise minimum
in the timeline clock and projects it back to each declaring clip. Each peer's
duck is active only during that peer's half-open authored lifetime. Linear duck
keys retain their slopes; adjacent representable times delimit boundary jumps
without a sample-rate-dependent ramp. Each clip keeps its own legacy fade product,
base gain, source trim and mix contribution. Unducked clips, different asset
selectors (even with identical filenames), tracks and shot occurrences remain
independent. This is derived plan data; compilation never edits the bundle.

`attach_compiled_audio` validates these bindings and exact envelope-host membership
before attaching to detached renderer copies. Remotion calls it after canonical
serialization; FFmpeg calls it before support and filter construction. Request
serialization, backend selection and replay retain the carrier and its identity.
The service shifts envelope keys and rebinds occurrence IDs for its existing
window projection, while finalizers consume already rendered audio artifacts.
Canonical save/load and undo preserve deliberately authored envelopes only; derived
narration/occurrence projections and their authority carriers never write back.
