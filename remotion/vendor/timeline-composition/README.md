# @banodoco/timeline-composition

Scaffold (Sprint 4). The eventual home of:

- `HypeComposition.tsx` and the rest of the Remotion composition.
- `EFFECT_REGISTRY` / `TRANSITION_REGISTRY` dispatch.
- The stable theme public API.

Sprint 4 ships **only the scaffold** plus the `./theme-api` sub-path —
re-exports of `effects.types`, `lib/animations`, and `ThemeContext` from
the in-tree `tools/remotion/src/` location. Theme components migrate off
the deep relative imports onto `@banodoco/timeline-composition/theme-api`
via the codemod at `tools/scripts/codemod-theme-api-imports.ts`.

The actual composition extraction lands in Sprint 5 (Phase 4d).

## Transitions

A clip's `transition` (on the clip that starts the boundary, i.e. the outgoing clip) joins it to the next clip on the same track. The next clip must start within one frame of the outgoing clip's end (or exactly at it); otherwise the transition is ignored and the boundary is a cut. A transition longer than either clip is also ignored.

- **Placement** is set by `align` on the transition object. `'end'` (the default, upstream's placement) puts the overlap window in the `T` frames before the boundary, so the incoming clip starts inside it. `'start'` puts it in the `T` frames after the boundary, and `'center'` centres it on the boundary.
- **Dissolve** (any id other than `fade`, e.g. `cross-fade`): the incoming clip ramps 0 to 1 over the window while the outgoing clip stays fully opaque underneath, so there is no hole in the frame.
- **Dip** (`fade`): the outgoing clip ramps 1 to 0 over the first half of the window, then the incoming clip ramps 0 to 1 over the second half. The background shows through at the midpoint, on purpose.
- **Extension frames**: a clip that is pulled into a window beyond its own span is drawn over the extra frames but shows itself frozen at its boundary frame, so its own timing never shifts. The outgoing clip is extended past its end; the incoming clip is extended back before its start.
- **Chains** (A to B to C): each boundary is planned independently. B is extended back for the A to B window and, if its own boundary to C runs past B's end, forward too. Each clip is drawn in track order, so later clips sit on top.

The planning is pure and lives in `typescript/src/lib/transition-spans.ts`.

## Pure planner API

Import `@banodoco/timeline-composition/evaluation` to prepare one visual track
and query individual frames without loading React, Remotion, theme components
or generated registries:

```ts
import {prepareTrack, queryTrackFrame} from '@banodoco/timeline-composition/evaluation';

const track = prepareTrack(
  [{start: 0, duration: 30}, {start: 30, duration: 30}],
  [{T: 10, kind: 'dissolve', align: 'end'}],
);
const at25 = queryTrackFrame(track, 25);
// {frame: 25, layers: [
//   {clipIndex: 0, drawLocalFrame: 25, ownFrame: 25, opacity: 1},
//   {clipIndex: 1, drawLocalFrame: 5, ownFrame: 0, opacity: 0.5},
// ]}
```

`prepareTrack(spans, transitions)` delegates to the existing `planTrack`. Inputs
are frame spans in track/painter order, with already resolved transition specs;
`transitions[i]` joins clip `i` to clip `i + 1`, and a missing/null entry is a cut.
The caller owns clip sorting, seconds-to-frames conversion, duration calculation
and theme transition resolution, as `TimelineComposition` does today. Prepare
again when these inputs change. The returned `plans` snapshot can be reused;
treat it and its nested plans as read-only.

`queryTrackFrame(track, frame)` accepts one finite track-absolute frame (including
fractional frames) and returns mounted layers in input order. Each layer retains
its input `clipIndex`. Mount intervals are `[drawStart, drawStart + drawDuration)`;
gaps and empty tracks return no layers. Mounted zero-opacity layers remain in
the answer. The result contains only the requested frame, with no film-length
enumeration or serialization. Preparation and each query scale with clip count,
not film duration; no cache is installed by this API.

`queryDrawFrame(plan, drawLocalFrame)` returns the same `drawLocalFrame`, clamped
`ownFrame`, and transition `opacity` for a single plan. It does not filter by
mount interval: its caller supplies a frame inside the clip's draw Sequence.
`TransitionLayer` uses this query for its Freeze frame and opacity;
`TimelineComposition` uses `prepareTrack` for its Sequences. Both paths delegate
to the original `ownFrame` / `layerOpacity` implementation. The subpath also
exports the planner input/output types, including `ClipSpan`, `TransitionSpec`
and `DrawPlan`.

This API describes transition mounting and timing. Its opacity excludes
track opacity, clip fades, component opacity, transforms, bounds, pixel coverage
and occlusion. It does not establish rendered visibility. Component internals
and coverage remain unknown here. Renderer-owner/Python caching and consumers,
coverage findings, and App Player integration are separate work.

## Speed

`clip.speed` is a playback rate, and the clip's length is its hold divided by `speed` (speed 2 halves the length).

- **Media clips** (`VisualClip`, `AudioTrack`) apply `speed` as `playbackRate` on the media itself, so the footage plays faster or slower.
- **Element clips** do not use `playbackRate`. The element sees `useCurrentFrame() = floor(local × speed)`, so speed 2 plays its animation twice as fast in half the length, and speed 0.5 plays it at half speed over twice the length. The entrance and exit windows stay in timeline frames, as before.
- A footage element (`am-footage`) reads the current frame for its video, so it plays at the clip's speed with no element change. An element must not set `playbackRate` itself, or the speed would apply twice.
- Invalid speeds (not a finite number above 0) act as 1.

The element remap is a `<Freeze>` around the element content, applied in `ClipLook.tsx`. The sanitising and frame math is pure and lives in `typescript/src/lib/clip-look.ts` (`clipSpeed`, `remappedFrame`).

## Build / test

```
cd packages/timeline-composition
npm install
npm run build
npm test
```
