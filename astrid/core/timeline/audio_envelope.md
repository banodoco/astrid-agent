# Compiled audio gain

`audio_envelope.py` defines transient `clip.audio.envelope` render output. It is
not an authoring field and must not be saved through the TimelineClip document
schema. Each envelope has ordered `keyframes: [{at, value, curve?}]`; a point's
curve controls its following segment, defaulting to `linear`. Times are seconds
relative to the compiled clip's timeline start. Outside the keys, endpoint gains
hold. Falling segments traverse the registry curve backwards, so equal-power
pairs preserve uncorrelated power. `multiply` composes other envelopes;
`minimum` takes the strongest of overlapping attenuations before multiplication.
All leaves use this same keyframe primitive, and all curve definitions come from
`audio_curves.json`. No expression or executable author data is accepted.

`compile_legacy(clip, fps)` projects rounded legacy fade windows, including the
product when fades overlap. Existing `audio.envelope` output takes precedence.
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
External Remotion encoding cannot consume these browser-local blob URLs and
refuses explicitly. FFmpeg instead evaluates the plan per sample with `aeval`,
then places clips at sample offsets and mixes without normalization. Both
consumers refuse unknown curve IDs (`ENVELOPE_UNRENDERED`). Missing narration
projection or unsupported duck consumers refuse with `DUCK_UNRENDERED`.

Remaining producer boundary: the authored TimelineClip schema does not admit
`audio`, and the existing saved-document render request loader does not carry a
separate compiled audio projection. The SDK adapter and pure compiled FFmpeg
builder can consume/produce it, but the shared timing/render-plan owner must
attach that output to its transient clips before both consumers receive them.
Raw authored duck tags refuse rather than silently rendering unducked. No new
persistent document shape or bed producer is supplied by this module.
