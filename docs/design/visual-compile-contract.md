# Visual compile contract

Status: settled MAIN image-cap reference for the Agent visual contract, from the 2026-10-20 popup/clip-look decisions, the exact native fixture, and the owner 11:59 reference. Golden vectors: `tests/editing_conformance/fixtures/visual_compile_contract.json`, run by `tests/editing_conformance/test_compile_contracts.py`. The settled pixel source is ORCH/design `viewing-main-image-cap-actual-reference.md`, operation `vw-m3-main-cap-locked-theme-stage-e2e-viewing-main6-locked-theme-image-cap-three-stills-1020115023-5447`.

Reference for "visible today": `origin/main` e51595c6. The compiler is `astrid/core/timeline/shot_composition_projection.py`, identical to main's file in this tree. The renderer is Composition 0.0.6 (`remotion/package.json:18` on main).

## 1. Popup closure: the native ADD fixture

The closure is the output of App's native `occurrence.add-media` on occurrence A, not a reconstruction. Source: App commit `309cb7a463833126e3af730fb3c6ff536d9e457c`, `src/tools/video-editor/commands/fixtures/recipe_m2_occurrence_media.json` (blob `22be20593b2b53dd35d1b0b71031f3cf128bf32e`), key `add.output`. The fixture stores that object verbatim as `native_add_output`.

### Closure

Two occurrences on the shared `picture` lane, both 10 s long:

| Occurrence | Shot (pinned revision) | Placement | Occurrence duration | Source window child (`clip-a`) |
|---|---|---|---|---|
| occ-a (A) | `shot-placement-17545…` (`authoring-shot-revision-e0bb…`, which adds `reserved-child`) | at 8 s (`placement.start_ms` 8000) | 10 s (`duration_ms` 10000) | at 1, hold 4, from 2, to 6, speed 2 |
| occ-b (B) | `chapter-shot` (`shot-1`) | at 10 s (`placement.start_ms` 10000) | 10 s (`duration_ms` 10000) | at 1, hold 4, from 2, to 6, speed 2 |

Other fields that the projection carries through:

- Each source window child has a timeline length of `hold / speed` = 4 / 2 = 2 s (`duration.py:113-142`). Its absolute window is therefore 9-11 s in A and 11-13 s in B.
- Each occurrence also has `clip-b` (at 5, hold 2, so 2 s). A's `clip-b` is 13-15 s, B's is 15-17 s.
- A has `reserved-child` (at 0.125, hold 4, from 2, to 6, speed 2, so 8.125-10.125 s). Its `opaque` is `[null, false, {}]` and its `params` is `null`. The projection keeps both.
- The parent's own `clip-a` and `clip-b` (on the same lane, at 1 and 5, not occurrence-owned) are excluded from the rows. They carry no `shot_occurrence_id`.
- Occurrence `speed` is 1 and `source_offset` is 0 on both occurrences.

### What main does with this closure

- The projection is `project_runtime_parent_composition`, which calls the same `_project_clip` as the reference. The visible window is `[occurrence start + child at, min(child end, occurrence end))` (`shot_composition_projection.py:308-313`).
- **The overhang clamp is not exercised by this closure.** No child extends past its occurrence end. Largest child ends: A 15 s against an end of 18 s; B 17 s against an end of 20 s. The rows are the same whatever the clamp setting. The clamp is exercised by vector 2 (section 1b), not by this native closure.
- **Occurrence spans overlap.** A spans 8-18 s and B spans 10-20 s, an 8 s overlap on the lane. Main accepts this and does not reject it. The source-window children do not overlap: A's child ends at 11 s, where B's begins. This is App's real output and stays as vector 1. Whether it is intended on a shared lane is open (section "Open"; App confirms the spans through the hub).
- The clamp decision is not settled by vector 1. The 03:46 "clamp is ON" decision in this document is a policy statement. App's `clampToOccurrenceDuration: false` (`ShotTimelinePreview.tsx:99`, `shotTimelineDataProvider.ts:404`) makes no difference to these rows.

### Rational clock

The native fps is `{"numerator": 30000, "denominator": 1001}` (NTSC, 29.97). Main's frame helpers take a Real. `Fraction(30000, 1001)` gives the right frames. Passing the `{numerator, denominator}` object, or the two-element `fps_rational` list, to `clip_start_frame` or `clip_end_frame` raises `ValueError: fps must be a number` at `_positive_fps` (`duration.py:45-49`). The test builds `Fraction(numerator, denominator)` from the fixture's `fps` and keeps the pair in the fixture. No frame is computed from a dict or list.

Frames are half-open, with JavaScript `Math.round` semantics (`duration.py:145-171`): `start = round(at * fps)`, `duration = max(1, round(visible_seconds * fps))`, `end = start + duration`.

### Physical reference (not produced here)

The physical reference is the approved executable analog (04:40 decision, Viewing's inbox): tiny generated playable media with the exact durations, explicitly labelled as an analog of the native fixture, plus the real host dispatch. MAIN is fed its own numeric fps, `Fraction(30000, 1001)`, and its frames are the reference. This worktree generated no media and rendered no frames. The reference belongs to Viewing, and it is valid only after MAIN's rational path and managed bytes are supplied. The rows below are the compiler's expectation for that reference.

Viewing's MAIN source probe (`handoff-vw-m3-shared-lane-main-vector-REPORT.md`) found that MAIN's own frame helpers return `NaN` for this fixture's rational fps, and that the fixture's `video/fixture` descriptors carry no playable bytes. So no MAIN frame value exists yet for either closure, and none is entered in the fixture.

### 1b. Overhang closure (vector 2)

Vector 2 is a labelled variant, not App output. It exists so that `clampToOccurrenceDuration` is actually exercised. The fixture is `overhang_closure`, and it is the native parent with only its occurrence list replaced.

- occ-c is on the picture lane at 2 s, 1.5 s long (window `[2, 3.5)` s). It pins the native `shot-1` revision.
- Its source-window child `clip-a` (at 1, hold 4, from 2, to 6, speed 2) is 2 s on the timeline, so absolute `[3, 5)` s. It overhangs the occurrence end by 1.5 s.
- **Clamp on** (the one policy the compiler has): visible `[3, 3.5)` s. Frames at 30000/1001 are `start 90`, `end 105`, `duration 15`.
- **Clamp off** (not a compiler mode): the unclamped child `[3, 5)` s. Frames would be `end 150`, `duration 60`. Decision (03:46, applied 06:18): there is no unclamped mode, so the two clamp-off rows are dropped from the fixture, not kept as xfails.
- **Physical reference: pending** (`overhang_closure.physical_reference`). It comes from Viewing's executable analog, as in section 1. No values are recorded.

Open against MAIN: Viewing's probe reports that MAIN's default projection (`clampToOccurrenceDuration` on) cuts the native closure's A children at 10 s, which is B's start, not at A's end (18 s). That is `occ-a:clip-a` `to 4` (1 s visible, end 10) against the compiler's `to 6` (end 11), and `reserved-child` `to 5.75` against `to 6`. The compiler clamps only to the occurrence end. Which of these is the intended clamp is open (section "Open", item 1). This document does not choose.

## 2. Element clips get the clip look (intended change from 0.0.6)

This is a deliberate difference, not lost behaviour (03:59 decision).

- **Main (Composition 0.0.6).** Effect clips are dispatched straight to the effect component and skip the wrapper. `docs/reference/timeline-composition-v0.0.6/TimelineComposition.tsx:121-132` returns `<EffectComponent/>` directly. Media clips go through `VisualClip`, which applies the fade (`VisualClip.tsx:57`, `lib/fade.ts:27-`).
- **Composition 0.0.7** (`6bd784d`, "clip look (opacity, crop, fades, entrance/exit) on element clips"). `TimelineComposition.tsx:81` in `v0.0.7` wraps effect clips in `<ClipLook kind="element">`. `ClipLook.tsx:40-48` applies opacity and crop to element clips only. Media clips get no opacity or crop from ClipLook, because `VisualClip` already handles their fade. But `ClipLook` wraps media clips too (`TimelineComposition.tsx:99,104,119`), so entrance and exit animations apply to media clips as well. Full list of 0.0.7 differences: `docs/design/owner-film-differences.md`.
- **Consequence.** Overlapping independent element clips with fades now DIP where main drew them opaque. The 16-30 DIP golden is approved as intended. Viewing lists the owner's films with overlapping element clips that use fades, opacity or entrance/exit (03:59 condition 3).
- **This tree.** `remotion/package.json:18` pins `file:artifacts/banodoco-timeline-composition-0.0.12.tgz` on current integration, so the intended change is already in the pin. `test_render_pin_gives_element_clips_the_clip_look` asserts the pin is at least 0.0.7.

### Proof still owed (not in this contract)

03:59 condition 1: a MAIN replay built from tag `v0.0.6` (git archive reproduces main's locked sha512) compared against the unified result, with real pixels rather than SSR markup. Only the intended clip-look differences may appear. That proof belongs to Viewing. This document does not claim it.

Also in the same 0.0.7 release, per-boundary transitions (dissolve vs dip) changed. The vectors here do not cover that.

## 3. Editor fade-clock analog (vector 3)

`cap_fade_closure` retains the full, opaque App `shotComposition.fixture.json`
from App commit `35ebae007d848ff22d8da89461bdb66efc93472f`, plus the explicit
synthetic child replacement from the independent review: an image at 0 s,
authored `hold: 5`, and `effects: {fade_out: 1}`. The existing
`withOccurrenceRevision` shape replaces only occ-1's child clips. This is a
synthetic counterexample, unlike vector 1's native ADD output; the original
external test bytes were not retained.

The authored occurrence still says 2000 ms. App derives the effective 4000 ms
cap from occ-2's next start on the same lane, against 5000 ms of content. The
corpus includes the actual default editor projection and the raw projection
from that frozen source. Both retain `canonicalTiming.durationMs: 5000` and
`canonicalTiming.occurrenceDurationMs: 4000`; their only clip difference is
`hold` (4 versus 5). The raw projection was obtained with the existing App
option as a diagnostic, not as a proposed per-call clamp policy.

The Agent analog receives the already-derived cap explicitly; it does not
reproduce App's lane-cap policy. The editor/read projection remains positively
clamped to `hold: 4`, while the authored source remains `hold: 5`. The fixture
preserves both complete projected objects and the original opaque fixture
fields.

| Input to public evaluation, 30 fps | Authored duration | Draw interval | Clip opacity at frame 105 (3.5 s) |
|---|---|---|---|
| Clamped editor analog: projected `hold: 4`, cap 4000 ms | 120 frames | [0, 120) | **0.5** |
| Raw `hold: 5` public-evaluator observation | 150 frames | [0, 120) | 1 (diagnostic only) |

The raw opacity `1` calculation is retained solely to document the pinned
public evaluator's authored-clock observation; it is not a product golden.
The analog agrees with the MAIN-confirmed `0.5`; the exact MAIN closure is
vector 4 below. Both draw plans contain frame 119 and exclude frame 120. The
`opacity` column is clip opacity, not a pixel/occlusion claim or the track
plan's transition opacity.
The contract tests extract this tree's pinned public package into a private
temporary directory and execute its pure `prepareScene`, `clipOpacity` and
`queryTrackFrame` exports with the incoming vector `[null]`. They use no local
fade formula. The Agent editor test supplies the already-derived 4000 ms cap to
the existing projector, preserves its positive clamp, and checks the authored
source graph remains unchanged. It does not force a cap in a production proof
or call the static playable-media analog an actual pipeline.

## 4. Exact single-occurrence MAIN cap (vector 4)

`single_occurrence_cap_closure` references the byte-identical
`fixtures/main_image_cap_raw_closure.json`, SHA256
`ad3d0ad35876665489080703c2323a8840c9187428dbe28a913e8ab4ada53f04`.
This is Viewing's captured synthetic MAIN closure: one occurrence with stored
`duration_ms: 4000`, placement start 0, and a pinned image with authored
`hold: 5` / `fade_out: 1`. There is no next occurrence. It is distinct from
vector 3, whose stored duration is 2000 ms and whose next cut is at 4000 ms.
The shared fixture includes the complete actual projected config, managed
registry and serialized timeline, without filtering opaque fields.

**The stored occurrence duration is the existing Agent render cap.** In
`astrid/core/timeline/shot_composition_projection.py`, `_occurrence_window`
reads `placement.start_ms` and `duration_ms`, giving [0, 4) seconds.
`project_runtime_parent_composition` passes that end to `_project_clip`, which
uses `min(child end, occurrence end)` and converts the visible duration back
to source units. Thus the projected image has hold4 and unchanged fade_out1.
The authored pinned child remains hold5. Neither content extent nor another
occurrence is consulted to derive this cap.

The executable regression `test_single_occurrence_cap_through_normal_managed_projection`
starts from those exact raw bytes through the existing in-process `_Runtime`
read fixture. It calls the normal `resolve_managed_render_snapshot` →
`_project_exact_parent_head` → `project_runtime_parent_composition` path in
`astrid/packs/rendering/actions/render/managed_timeline.py`, validates the
snapshot and materializes it. The normal `_serialize_timeline` in
`astrid/packs/rendering/shared/rendering_backend/__init__.py` invokes
`Timeline.for_render` and retains hold4. Direct projection and the managed
path must both equal the full shared fixture. No cap or occurrence is
injected into this test, and input bytes and objects must remain unchanged.

The actual serialized clips and managed registry then enter the pinned
Composition 0.0.12 public `prepareScene`, `queryTrackFrame` and `clipOpacity`
exports, extracted into a private temporary directory. This closure has no
transitions; preparation receives no synthesized incoming cap metadata.
Results at numeric30fps: own/draw duration120, f90 opacity1, f105 opacity0.5,
f119 present with opacity1/30, f120 absent. Calling `clipOpacity` on the raw
hold5 child gives1 at105; that is explicitly diagnostic only.

This replaces candidate6749e542's strict production xfail, which evaluated
raw hold5 directly and therefore did not exercise the production join.
The real pipeline already satisfies the required0.5, so no Agent product,
SDK timing compiler, package pin, field or default changes are needed.
The existing prepared-editor positive in section3 remains unchanged.

The original MAIN pixel reference is
`viewing-main-image-cap-actual-reference.md` in the orchestrator design
folder, central operation
`vw-m3-main-cap-locked-theme-stage-e2e-viewing-main6-locked-theme-image-cap-three-stills-1020115023-5447`.
Frame90 RGB240 and frame105 RGB122/124/130 over background5/8/20 support
opacity0.5; frame105 PNG SHA256 is
`c0b0adc7856e2750137315451a9657661925436746d0da1e9cd6a2527d14431b`.
The reference's exact PNG hashes are retained in the shared fixture.
No pixels were rerun here. This pure preparation/evaluation proof does not
claim live Runtime acceptance, renderer execution, App preview/export
parity, audio or Gate6 closure.

## Vectors

The original frame rows are the source-window child of each occurrence. Vector 1 is the six native rows (section 1). Vector 2 is the overhang closure (section 1b). Each row is a frame value from the projected clip (`shot_occurrence_id` plus `app.astrid_shot_composition.source_clip_id` = `clip-a`). A row with `closure` = `overhang` is vector 2. Vector 3 is stored separately as `cap_fade_closure` and checks the raw authored fade clock against the bounded draw interval (section 3). Vector 4 is `single_occurrence_cap_closure`, the exact MAIN closure through normal managed preparation (section 4).

| id | Occurrence / child | Field | Expect | Status |
|---|---|---|---|---|
| popup-occurrence-a-start-frame | occ-a / clip-a (9 s) | start_frame | 270 | pass |
| popup-occurrence-a-end-frame | occ-a / clip-a (11 s) | end_frame | 330 | pass |
| popup-occurrence-a-duration-frames | occ-a / clip-a | duration_frames | 60 | pass |
| popup-occurrence-b-start-frame | occ-b / clip-a (11 s) | start_frame | 330 | pass |
| popup-occurrence-b-end-frame | occ-b / clip-a (13 s) | end_frame | 390 | pass |
| popup-occurrence-b-duration-frames | occ-b / clip-a | duration_frames | 60 | pass |
| `test_closure_places_occurrences_at_eight_and_ten_seconds_on_the_picture_lane` | occ-a at 8 s, occ-b at 10 s, both 10 s | placement | as stated | pass |
| `test_popup_closure_source_windows_are_half_open_on_the_shared_lane` | A's child end <= B's child start | order | 330 <= 330 | pass |
| `test_native_child_keeps_its_opaque_and_null_params_fields` | occ-a / reserved-child | `opaque`, `params` | `[null, false, {}]`, `null` | pass |
| `test_render_pin_gives_element_clips_the_clip_look` | pin >= 0.0.7 | pin | >= 0.0.7 | pass |
| overhang-clamp-on-start-frame | vector 2, occ-c / clip-a (3 s) | start_frame | 90 | pass |
| overhang-clamp-on-end-frame | vector 2, occ-c / clip-a (visible end 3.5 s) | end_frame | 105 | pass |
| overhang-clamp-on-duration-frames | vector 2, occ-c / clip-a | duration_frames | 15 | pass |
| `test_overhang_child_is_clamped_to_its_occurrence_end` | vector 2, occ-c / clip-a | at, visible duration | 3.0 s, 0.5 s | pass |
| `test_cap_fade_editor_projection_keeps_clamped_timing_and_authored_source` | vector 3, occ-1 / overlong-child | projected hold / authored hold | 4 s / 5 s | pass |
| `test_cap_fade_public_source_clock_and_half_open_draw_cap[clamped-analog]` | vector 3, clamped editor analog | 0.5 / 120 / 120; frame 119 present, frame 120 absent | pass |
| `test_cap_fade_public_source_clock_and_half_open_draw_cap[editor]` | vector 3, editor projection | 0.5 / 120 / 120; frame 119 present, frame 120 absent | pass |
| `test_cap_fade_public_source_clock_and_half_open_draw_cap[raw-diagnostic]` | vector 3, raw authored input | 1 / 150 / 120; diagnostic only | pass |
| `test_single_occurrence_cap_through_normal_managed_projection` | vector 4, exact stored4000/hold5 closure | projected hold4; f90=1, f105=0.5, f119 present, f120 absent | pass |

The six native rows are unchanged. Every clamp-on row is produced on this branch, and the clip look is already in this tree. No rows were added or kept as xfails for clamp off: the compiler has no unclamped mode, and the decision does not add one.

The settled focused run is recorded in the worker report. Audio vectors 10–11
are covered by the existing trusted normal-audio preparation boundary; this
visual contract does not claim audio PCM parity. The single-occurrence visual
cap regression is an ordinary passing assertion.

## Open

1. **Clamp: which clamp is intended.** Two items, both for the orchestrator.
   - Whether an unclamped mode is wanted at all. Decided 06:18: the 03:46 decision holds, so the clamp-off rows are dropped rather than kept as xfails.
   - MAIN's default clamp (Viewing's probe) does not match the compiler on vector 1. MAIN cuts A's children at B's start (10 s); the compiler cuts at A's end (18 s). Viewing's evidence says the MAIN clamp for the native closure ends at 10 s, not 18 s. Not verified from source here.
2. **Overlapping occurrence spans on the picture lane.** A (8-18 s) and B (10-20 s) overlap by 8 s. Main accepts it, and the source-window children do not overlap. App confirms the spans through the hub (orchestrator asked, 04:57). Vector 1 stays as native until then.
3. **App single-occurrence effective duration.** The App report
   `app-main-cap-half-golden.md` shows that its current consumer replaces the
   exact stored4000ms cap with content5000ms when no next occurrence exists.
   Agent already honors stored4000ms through normal preparation. App policy
   reconciliation belongs to the App owner/orchestrator; preserve its original
   stored2000ms/next4000ms editor positive as well as this exact closure.

## Not verified

- The Agent worker did not rerun MAIN capture; the settled pixel values and
  hashes are imported from the ORCH/design reference named above. The vectors
  test normal Agent managed projection/materialization/serialization and the
  pinned public evaluator; no renderer/browser or live Runtime was started.
- App's tests were not run from this worktree. App's own clamp replica (`src/tools/video-editor/data/shotCompositionProjection.ts:437-449`) and its `clampToOccurrenceDuration: false` callers were not changed here.
