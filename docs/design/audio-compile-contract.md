# Audio compile contract

Status: contract draft for the Agent compiler, from the 2026-10-20 03:17, 03:20 and 04:24 orchestrator decisions. Golden vectors: `tests/editing_conformance/fixtures/audio_compile_contract.json`, run by `tests/editing_conformance/test_compile_contracts.py`.

Owners: Agent (one compiler, export path), App (preview adapter), Viewing (PCM goldens and instrument).

Reference for "audible today": `origin/main` e51595c6. The Python compiler is `astrid/core/timeline/shot_composition_projection.py`. Playback is Composition 0.0.6 `AudioTrack.tsx`, which is what `remotion/package.json:18` pins on main and `docs/reference/timeline-composition-v0.0.6/AudioTrack.tsx` copies.

## Decisions this contract applies

1. **One export contract, three projections.** Agent owns the export projection rules. App and Runtime retain their own projection implementations; shared golden vectors assert that the three projections agree on the covered timing and media-kind behavior. The vectors establish conformance for those cases, not a single shared code path.
2. **Gain is multiplicative**: `child x occurrence`. Examples: 0.8 x 0.5 = 0.4; 1.5 x 2 = 3.
3. **For audio clips, `source_offset` and occurrence `speed` follow main's audible playback**, not App's former adapter. Main ignores both on the playback path, so the compiler keeps them as metadata. Metadata-only is acceptable here only because main does not change what is heard. Visual clips use the media-specific placement rule below.
4. **Fades use one continuous, source-time envelope** for App preview and Agent export. The public Composition `applyAudioEnvelope` is the policy (03:20). The normal render route now crosses the trusted prepared-source boundary; pure projection still cannot invent `audio.render_source`, and an unprepared or mismatched source refuses `ENVELOPE_UNRENDERED`.
5. **Fade `curve=` is HONOURED in the unified path** (orchestrator 04:24Z). The author's `curve=NAME` is kept as `params.fadeInCurve` / `params.fadeOutCurve` and the envelope applies it. Main's playback ignores it, so main's audible fades are always linear. This is a deliberate change for films that use `curve=`; they are listed in `docs/design/owner-film-differences.md`.

## What main actually does (evidence)

| Field | Compiler output on main | Playback on main | Contract |
|---|---|---|---|
| Gain | `shot_composition_projection.py:359-363`: `clip.volume = clip.volume (default 1) * occurrence.gain`, written only when the child has a volume or the gain is not 1 (so an absent volume stays unity, not an invented 1.0) | `AudioTrack.tsx:43-45`: `baseVolume = track.volume * clip.volume`, zero when the track is muted | Multiplicative. **Applied.** |
| Mute | `shot_composition_projection.py:364-365`: `volume = 0` | same `baseVolume` path | Silence. **Applied.** |
| `source_offset` | `shot_composition_projection.py:353`: kept only in `app.astrid_shot_composition.source_offset`. The child window is computed from the child's own `from`/`to`/`hold` (`:316-326`). | `AudioTrack.tsx:75` uses `computeMediaTrim(clip)`, and `lib/trim.ts:4-18` reads only `clip.from` and `clip.to`. The occurrence offset is never read. | Metadata only. **Not applied.** `tests/timeline/test_shot_composition_projection.py:47` asserts `from == 2` while `:50` keeps `source_offset == 7` as metadata. |
| Occurrence `speed` | `shot_composition_projection.py:305-307` says the placement speed is a placement property and the child's speed stays intact. `:316` reads child speed only. `:354` stores the occurrence speed as metadata. | `AudioTrack.tsx:76`: `playbackRate = clip.speed` (child only) | Metadata only. **Not inherited.** |
| Fades (`params.fadeIn/fadeOut`) | The duration is stored as the value. `clip_fields.py:358` drops the curve from the value (`raw, _curve = _audio_fade_parts(raw)`), but the curve itself is persisted at `params.fadeInCurve` / `params.fadeOutCurve` (`clip_fields.py:513-518`, written by `cli.py:3069-3070`; absent means linear). | `AudioTrack.tsx:48-70`: linear only, with a per-frame `volume(frame)` callback (`frame / fadeInFrames`). The stored curve never reaches playback. | Target: continuous source-time envelope (decision 4) with the curve applied (decision 5). This is a deliberate change from main's frame-step, recorded as ledger row VW-AUDIO-STEP. Main's per-frame step can be up to about 0.7 dB off mid-fade. |

`AudioTrack.tsx` is identical between tags `v0.0.6` and `v0.0.7` in the composition repo (`git diff --stat v0.0.6 v0.0.7` is empty). I did not read the pinned 0.0.8 artifact from this sandbox, so for 0.0.8 the statement is unverified.

## Clock inputs for the envelope

The source clock follows what main plays, and nothing else:

- `source_from` = the public `computeMediaTrim(compiledClip, fps).trimBefore / fps`, not raw `from`. The public helper rounds to a frame (`composition/typescript/src/lib/trim.ts`), so `from=0.125` gives trimBefore 4 at both 30 and 30000/1001 fps and the origin is `4/30` or `4/(30000/1001)`; `from=0.05` gives trimBefore 2 at 30 and 1 at 30000/1001. Those distinct origins must reach prepared-source admission unchanged.
- `playback_rate` = the public `getSanitizedPlaybackRate(compiledClip.speed)` (the child's `speed`; absent means 1).
- The occurrence `source_offset` and occurrence `speed` are **not** clock inputs (decision 3).
- Gain is never part of the clock. Constant base gain (`track.volume * clip.volume * occurrence.gain`) is passed separately from the envelope.

## Curve semantics (decision 5)

The public package already reads the curve from the same place main writes it:

- `composition` HEAD `typescript/src/lib/audio-envelope.ts` (`compileAudioEnvelope`): for each of `fadeIn` / `fadeOut`, the curve is `params[`${name}Curve`] ?? 'linear'`, carried on the keyframe that starts the ramp. Fade length is `Math.round(raw * fps) / fps`.
- Curve names and shapes match the Python registry `astrid/core/timeline/audio_curves.json` and `audio_curves.gain()`: `linear`, `equal-power` (sin), `exponential-fast` / `exponential-slow` (`expm1` form), `s-curve`. A fade-out mirrors progress as `1 - t` in both.
- Compiler output therefore only has to keep `params.fadeInCurve` / `params.fadeOutCurve` on the clip. Linear is the absence of the key, as `clip_fields.py:515-516` already writes it.
- Historical note (2026-10-20): the pinned 0.0.10 package's copy of `audio-envelope.ts` was not read; the Composition repository HEAD was read instead. The current approved 0.0.14 profile is covered by the trusted preparation proof below.

## Golden vectors (15)

Vectors 1-11 and 15 are the shared audio set; each is reconciled against Viewing's authored 11 in the table below. Vectors 12-14 are Agent-authored for decision 5.

| # | id | Input (child / occurrence) | Expected | Status |
|---|---|---|---|---|
| 1 | gain-child-x-occurrence | volume 0.8 / gain 0.5 | volume 0.4 | pass |
| 2 | gain-boost-x-occurrence | volume 1.5 / gain 2 | volume 3 | pass |
| 3 | gain-child-absent-is-unity | absent / gain 0.5 | volume 0.5 | pass |
| 4 | gain-unity-keeps-volume-absent | absent / gain 1 | no volume key | pass |
| 5 | gain-zero-is-silent-not-absent | absent / gain 0 | volume 0 | pass |
| 6 | muted-occurrence-silences-child-gain | volume 0.8 / gain 0.5, muted | volume 0, muted true | pass |
| 7 | source-offset-is-metadata-only | trim 0.125..1.125 / source_offset 350 (ms) | trim unchanged; offset 350 as metadata | pass |
| 8 | occurrence-speed-not-inherited | child speed absent / speed 1.5 | speed absent; 1.5 as metadata | pass |
| 9 | child-speed-wins-over-occurrence-speed | speed 2 / speed 1.5 | speed 2; 1.5 as metadata | pass |
| 10 | fade-in-needs-prepared-envelope-source | params.fadeIn 0.5 | `audio.render_source` present after trusted preparation | pass |
| 11 | fade-out-needs-prepared-envelope-source | params.fadeOut 0.5 | `audio.render_source` present after trusted preparation | pass |
| 12 | fade-in-curve-is-kept-on-the-compiled-clip | fadeIn 0.5, fadeInCurve equal-power | `params.fadeInCurve` equal-power | pass |
| 13 | fade-out-curve-is-kept-on-the-compiled-clip | fadeOut 0.5, fadeOutCurve exponential-slow | `params.fadeOutCurve` exponential-slow | pass |
| 14 | linear-fade-has-no-curve-key | fadeIn 0.5, no curve | `params.fadeInCurve` absent | pass |
| 15 | gain-unity-keeps-child-volume | volume 0.8 / gain 1 | volume 0.8 | pass |

Vectors 1-9 and 12-14 pass on the current projection. Vectors 1-9 are main's behaviour. Vectors 12-14 pin the curve as metadata that survives compilation. Vectors 10-11 pass when the projected clips are handed to the existing trusted normal-audio preparation boundary; they do not claim that pure projection itself creates a source. The preparation path uses the public envelope policy and attempt-owned output, while the direct consumer still rejects an absent or mismatched prepared source.

The vector test now checks an absent dotted key with `_lookup(...) is None`. The old `key not in clip` check only looked at top-level keys and would have passed for `params.fadeInCurve` regardless of its value.

The occurrence-offset and occurrence-speed rows (7-8) pin the audio-only metadata rule. App and Runtime apply the same media-specific split; their visual projections apply occurrence placement as described below.

## Media-specific occurrence timing

The occurrence timing fields have distinct playback rules by media kind:

- **Visual clips:** add the occurrence's millisecond `source_offset` to the child's source window once. A child `speed` takes precedence; otherwise inherit the occurrence `speed`. Clip duration and the applicable occurrence cap are computed with that effective speed.
- **Audio clips:** leave the occurrence `source_offset` and occurrence `speed` as metadata. Preserve child trims and child `speed`; an absent child speed keeps the normal playback default of 1.
- A declared domain track kind (`audio` or `visual`) classifies the clip first, regardless of the track ID. Parent output-track definitions win an ID collision; otherwise the child's declared track kind is carried into the projection. When no usable track kind exists, an explicit audio clip type or the selected asset's audio media type can identify audio. Asset fallback requires an exact asset ID match, and track-name strings do not determine media kind.

The byte-identical vector set in `tests/editing_conformance/fixtures/occurrence_media_projection.json` is exercised through the Agent's canonical and Runtime-parent projectors, and copied unchanged to the App and Runtime test fixtures. It covers a nonzero trim and offset, inherited and child-overridden rates, trim and hold caps at a two-second occurrence window aligned with the next same-lane occurrence, audio classification from a child-only track with no clip or asset hint, parent-kind precedence on a same-ID collision, a nonstandard audio track ID, audio-kind inference from asset and clip metadata, unchanged standalone parent media, and preserved source registry identity.

### Reconciliation with Viewing's authored 11 (2026-10-20)

Source: Viewing's `shotCompositionAudio.fixture.json` (11 rows, base clip `from 0.125, to 1.125`) and its `audio-corpus-map.json`. No pair of complete authored inputs is identical to ours, so "same" means the same rule on the same occurrence/child values, not the same graph. Both sets stay as authored; nothing is converted to make them agree.

| Viewing original | Ours | Verdict |
|---|---|---|
| 1 unity gain, child 0.8 | 15 gain-unity-keeps-child-volume (added) | same rule: volume 0.8 |
| 2 absent child volume, gain 0.5 | 3 gain-child-absent-is-unity | same: 0.5; trim and placement differ |
| 3 mute, child 0.8, gain 0.5 | 6 muted-occurrence-silences-child-gain | same: volume 0, muted metadata |
| 4 child 0.8 x gain 0.5 | 1 gain-child-x-occurrence | same: 0.4 |
| 5 child 1.5 x gain 2 | 2 gain-boost-x-occurrence | same: 3 |
| 6 source_offset **350** | 7 source-offset-is-metadata-only | **differed in value** (ours was 0.35); now 350, see the unit decision below. Same rule: trim unchanged, offset kept as metadata |
| 7 occurrence speed 1.5, child absent | 8 occurrence-speed-not-inherited | same: child speed absent, 1.5 metadata |
| 8 hold 10 capped at next occurrence | none | Viewing-only: a projection cap, not audio policy. Covered by Viewing's corpus map (hold 7.5); not duplicated |
| 9 trim to 10.125 capped | none | Viewing-only, same reason (to 7.625) |
| 10 params fades 0.1 in/out on .05..0.55 | 10 and 11 (fade 0.5, from 0) | differs: combined vs separate, other durations and origin. The authored combined case remains a clock counterexample; the two current vectors use the trusted preparation boundary. Viewing's `.05` origin is the clock counterexample above |
| 11 `effects:[{fade_in,fade_out}]` | none | Viewing-only: opaque shape, no public envelope, no compiler rule to pin |

Ours without a Viewing counterpart: 4 (unity keeps volume absent), 5 (gain 0 is silent), 9 (child speed beats occurrence speed), 12-14 (curve). No disagreement on any scalar rule (gain, mute, metadata-only offset/speed).

**Unit decision: occurrence `source_offset` is milliseconds. 350 is canonical; 0.35 was our error.** Evidence in the tree:

- Runtime `runtime_protocol/visual_seam.py:291-294` reads `source_offset` (alias `source_offset_ms`, or the `{start,end}` start) and divides by 1000.
- App `shotCompositionProjection.ts:361` converts `sourceOffsetMs / 1000` to seconds; `shotCompositionAdapter.ts:245-246` feeds it `occurrence.source_offset`.
- Agent `shot_composition_migration.py:779` accepts the legacy name `source_offset_ms`; authoring placements carry `source_offset: {start, end}` beside `duration_ms`.
- Agent `shot_composition_projection.py:353` passes the value through verbatim, so the compiler is unit-agnostic. Only the consumers that apply it scale, and they scale from milliseconds. Nothing treats it as seconds.

Our 0.35 read a milliseconds field as seconds. The compiler result is identical either way (metadata only), so only the fixture value and this note change.

## ENVELOPE_UNRENDERED preparation boundary

Boundary: the public `applyAudioEnvelope` path needs a trusted prepared source. Pure logical projection has no file authority and must not manufacture `render_source`. The normal render route prepares managed audio in `root_preparation.py` by calling `prepare_normal_audio_sources` from `normal_audio.py`; that helper verifies the managed bytes and registry, invokes the profile's Composition envelope implementation, and keeps the result under the attempt-owned serving root. A direct consumer with no matching prepared source still refuses `ENVELOPE_UNRENDERED`; relaxing that check, removing the fade, or adding a substitute producer is not allowed.

Preparation path (Agent side; the 03:17 decision requires Viewing to consume this boundary):

1. **Compile.** A fade-bearing audio clip gets an exact envelope identity from the public `compileAudioEnvelope(clip, fps)`, and the clock from the rules above. No App-side envelope selection.
2. **Prepare (trusted host, normal render route).** Decode the source once per exact tuple `(source URL, envelope identity, source_from, playback_rate)`, using the public Composition `applyAudioEnvelope` with the bounded decode. Write the result with `audioEnvelopeWav` into an attempt-owned file. Record it as `clip.audio.render_source = {file, envelope_key, source_from, playback_rate}`. This field is prepared output and is never authored; App must not create it.
3. **Consume.** The public `encodedEnvelopeSource` accepts only an exact match of that tuple and throws `AudioEnvelopeError` otherwise. A forged or mismatched `render_source` refuses; it never plays.
4. **Unprepared inputs.** The direct consumer keeps refusing `ENVELOPE_UNRENDERED`; nothing falls back to the frame-step callback. The two contract vectors cover the prepared normal-route boundary, not every render or PCM-parity path.
5. **Ordering.** The 03:17 decision pairs this with the slice/layer P1 fix. Viewing's helper must consume the Agent-prepared boundary rather than inventing an independent source.

**Current status.** The approved profile exports `applyAudioEnvelope`, `audioEnvelopeWav`, and `encodedEnvelopeSource` through its public evaluation module. The normal render route has the producer and the bounded fixture verifies real managed WAV bytes, registry digest identity, attempt-local serving, and the two original fade assertions. This is not a claim of renderer/browser execution, App preview/export parity, or PCM-golden closure. The four source-phase tests in `test_normal_audio_source_phase.py` remain separate acceptance for sliced-source PCM preservation.

## Ledger and owner items

- VW-AUDIO-STEP: App `AudioTrack.tsx` per-frame callback and the old frame-table comparator are retired only after Agent's compiled clock is published, App's PCM adapter is live, both consumers use it, and Viewing's Player-vs-export PCM goldens pass with no threshold widening. The frame-step RED baseline stays as a record.
- Owner review at the hard stop (per the PLAN): films with fades will sound about 0.7 dB different mid-fade. List them.

## Open items for the orchestrator (not decided here)

1. **`curve=` on audio fades: RESOLVED, honoured** (decision 5, orchestrator 04:24Z). Owner visibility: the films that use `curve=` are listed in `docs/design/owner-film-differences.md`. That scan found no owner film in reach, so the list is open.
2. **Viewing's 11.** Reconciled above; the unit question is closed (milliseconds).

## Not verified

- The bounded fixture rendered attempt-local PCM for source admission, but no Player/export PCM comparison was performed. The vectors do not claim renderer/browser execution or parity.
- The pinned 0.0.8 artifact's `AudioTrack` was not read (see above).
- App's tests were not run from this worktree.

## Slice phase (PCM source-phase preservation)

`RenderService._window_clip` rewrites `at`/`from`/`to`, so a fade restarts at the cut: a 0..2 s clip with `fadeIn` 0.5 sliced at 0.5 s has (.25, -.125) at source 0.5 in the original and (0, -0) in the slice. Two different originals can slice to the identical clip, so the phase cannot be recovered afterwards. The host slicer now records `app.astrid_slice_origin = {id, at, from, to}` on fade-bearing clips (host-owned; any authored value is dropped).

For sliced inputs, the prepared-source producer must compile E0 once from the original clip, take `q0`/`q1` from the public rounded `computeMediaTrim` of original and sliced clip, `r` from the child speed, and translate every keyframe `at` of E0 by `-(q1-q0)/r` into the derived `audio.envelope`. Then source sample 15000 of the slice equals the original. The four PCM tests in `tests/packs/rendering/test_normal_audio_source_phase.py` are separate source-phase acceptance; this bounded contract proof does not claim them.
