# Worlds: authoring explorable live scenes

> **Worlds are always explorable. Shots live within them.**

A world is one self-contained Three.js live-scene package: a single HTML
entry, run in a sandboxed iframe, that implements `window.astridScene`. Its
living state (water, creatures, weather, light) is evaluated from time alone,
and it can be viewed from anywhere inside its bounds. The authored shots are
camera moves placed inside that world.

The rule is a restriction that removes problems instead of adding work:

- **You build a place, not a set of fronts.** Because any viewpoint must hold
  up, there is no backstage to keep consistent with the shots. Painted
  backdrops that end at the frame edge, objects that exist for one angle, and
  a sky that is only the background colour behind one shot are not available,
  so the bugs they cause never happen.
- **One world, three readers.** The World Explorer, the timeline preview and
  the final video run the same code on the same world. A fix to the world shows
  in every shot at once.
- **Shots are cheap.** A shot is a name, a source-time range and a pose.
  Adding one, retiming one, or filming an event again from another angle never
  touches the world.
- **Edits stay local.** World edits and shot edits live in separate,
  named parts of the source, so a person or an agent can change one without
  silently moving the other.

Use this guide whenever you create, convert or edit a world (a
`haus.astrid.liveScene` clip). Use the ordinary
[timeline editing guide](../../../../../docs/timeline-editing-guide.md) for
clip placement, trim, playback rate and publication; a world does not add a
second timeline or retiming system. Start from the
[world template](../templates/world-template.ts).

The loader, explorer and renderer belong to the `rendering` pack: the declared
editor entry is
[`ui/threejs-live-editor/extension.tsx`](../../ui/threejs-live-editor/extension.tsx)
("Three.js Live Editor", which also contributes the World Explorer panel), and
the pack's `pack.yaml` remains the discovery authority. A project world's HTML,
data, immutable objects and revisions belong to Runtime project storage.
Editing a world does not rebuild the editor. Do not edit a checkout database,
generated catalog, private store or mutable dev URL.

## World, shots, view

| Term | Meaning |
| --- | --- |
| World | The whole package: one HTML entry implementing `window.astridScene`. |
| World time | The clock of the world's living state and events, in seconds from `0` to the manifest duration. |
| Event | A named thing that happens in the world, at a world time (a point) or over a world-time range. Events happen whether or not any shot frames them. |
| Source time | The clock the timeline plays: the authored sequence of shots, from `0` to the manifest duration. |
| Shot | A named, authored stretch of the sequence: a source-time range, the world time it shows, and a camera pose. |
| View | A free viewpoint, `{position, quaternion, fov}`, that the World Explorer passes in. |
| Explore | The world's declaration of where the free view starts and where it may go. Required. |

Every frame comes from one of two paths:

```text
Timeline and video   sourceTime ─► the shot covering it
                                ─► worldTime = worldStart + (sourceTime − start)
                                ─► world state at worldTime, drawn from the shot's pose

World Explorer       worldTime + view
                                ─► world state at worldTime, drawn from exactly that view
```

The World Explorer has two modes. **Explore** moves freely through the world
(look, move, up and down, speed) while world time plays on a loop, with pause.
**Shots** plays the authored sequence and lets the user pick a shot and
place a stretch of it on the timeline. The timeline plays the world along its
shots, and that is what ends up in the video.

## The scene interface

The entry sets `window.astridScene` before or during `initialize()`:

```ts
type Vec3 = [number, number, number];

interface AstridScene {
  // Required: where the free view starts and where it may go.
  explore: {
    start: {position: Vec3; target: Vec3};
    speed?: number;                    // world units per second
    bounds?: {min: Vec3; max: Vec3};   // the host keeps the view inside these
  };
  // The authored shots, in SOURCE seconds. None means one implicit shot
  // covering the whole duration, with world time equal to source time.
  shots?: ReadonlyArray<{name: string; start: number; end: number; worldStart?: number}>;
  // What happens in the world, in WORLD seconds. A point event omits `end`.
  events?: ReadonlyArray<{name: string; start: number; end?: number}>;

  initialize(): Promise<void>;
  render(frame: {
    sourceTime: number;
    width: number;
    height: number;
    view?: {position: Vec3; quaternion: [number, number, number, number]; fov: number};
    worldTime?: number;
  }): Promise<void>;
  dispose(): void;
}
```

What each host sends to `render`:

- **Timeline preview and final video** send only `{sourceTime, width,
  height}`. Draw the authored shot that covers `sourceTime`, at the world
  time that shot shows. This path must be deterministic.
- **World Explorer** sends `{sourceTime, width, height, view, worldTime}`,
  with `sourceTime` equal to `worldTime`. Evaluate the world at `worldTime`
  and draw from exactly `view`. Do not clamp, smooth or adjust the view: the
  host has already kept it inside the bounds.

Conventions: Y is up, and an unrotated camera looks down −Z (Three.js
defaults). `quaternion` is `[x, y, z, w]`. `fov` is the vertical field of view
in degrees; the explorer currently uses 60, which is usually wider than an
authored shot. Choose world units and keep them consistent; metres read well.

### What the host keeps

The bridge reads `explore`, `shots` and `events` once, after `initialize()`
resolves and fonts and document images are ready, and returns them on the
`initialized` acknowledgement. The host checks them again. Invalid entries are
dropped silently and never fail the world, so check what was kept (the
explorer shows it).

- **`explore`**: `start.position` and `start.target` are finite `[x, y, z]`
  and not equal. `speed`, if given, is `> 0` and `<= 10000`; without it the
  explorer moves at the largest bounds extent divided by 8 per second (4
  without bounds), and the user can scale it. Pick `speed` so a walk to the
  far side of the area you want visitors to see takes about 5 to 6 seconds
  at 1x (Lantern Valley's village is about 320 m from its start, so it uses
  60). Too slow reads as stuck, and the user can only scale by a few steps.
  Movement eases in and out, so the speed is a top speed. `bounds.min` must be below
  `bounds.max` on every axis. A start position outside the bounds is clamped
  into them. If any part is invalid, the whole declaration is dropped and the
  world counts as not explorable: the explorer opens it in Shots mode only.
- **`shots`**: at most 32 (a longer array means none). Each has a trimmed
  name of 1–80 characters and `0 <= start < end <= duration`. `worldStart`,
  if given, is in `[0, duration]`. Shots are sorted by `start`, and a shot
  that overlaps an earlier one is dropped. The host does not check that
  `worldStart + (end − start)` stays within the duration. Keep it within, because
  world time never runs past the duration.
- **`events`**: at most 64 (a longer array means none). Each has a trimmed
  name of 1–80 characters and `0 <= start <= duration`; `end`, if given, is
  `>= start` and `<= duration`. Events are sorted by `start`.
- **`view`**: the host normalises the quaternion and clamps `fov` to 10–120.
  An invalid view is dropped, and the world then draws its authored camera. A
  `worldTime` outside `[0, duration]` is dropped.

These declarations are metadata. They do not change the package manifest,
digest or any timeline timing. The manifest keeps exactly its four fields;
`explore`, `shots` and `events` live in the entry, declared by the running
world, so the explorer and the timeline read the same truth.

## Write a world in four layers

The [world template](../templates/world-template.ts) is a small pond world
with two events and two shots. It shows every layer below, and its frames
hold up from any viewpoint inside its bounds.

### 1. The world constructor

Build the place once, in `initialize()`: renderer, scene, sky, ground, things,
lights. Record every geometry and material you create so `dispose()` can
release them. Build nothing for one shot only: everything in the world is
there for every viewpoint. Do not start `requestAnimationFrame`, timers that
drive state, input handlers, native media playback or an audio clock.

### 2. World state from world time, including events

`evaluateWorld(world, worldTime)` sets every animated property from
`worldTime` alone. Derive or reset each property on every call and never
accumulate from the previous frame, so a direct seek and a backward seek give
the same frame. Do not read `Date`, `performance.now()` or unseeded
`Math.random()`; seed any randomness from author-controlled inputs.

Declare each event once and have the behaviour read that same object. In the
template, `BOAT_CROSSES = {name, start, end}` is both the declared event and
the range the boat's motion reads, so retiming the event moves both together.
Keep independently editable events separate even when their times coincide.
An agent can then retime one without silently moving the other. An event
happens whether or not any shot frames it: the template's lanterns light at
world time 7, which no shot shows but exploring does.

### 3. Shots: source time to world time and pose

A shot maps a stretch of source time onto the world:

```ts
const s = clamp(sourceTime, 0, DURATION);
const shot = SHOTS.find(c => s >= c.start && s < c.end) ?? SHOTS[SHOTS.length - 1];
const worldTime = (shot.worldStart ?? shot.start) + (s - shot.start);
```

- Cover `0..duration` with shots, with no gaps and no overlaps, so every
  source time has exactly one shot. Ranges are half-open, and the final
  instant belongs to the last shot.
- A shot without `worldStart` is **aligned**: world time equals source time.
- `worldStart` lets a shot show a different stretch of world time. Two
  shots can show the same event from different angles. In the template,
  "Wide on the crossing" (source 0–5, aligned) and "Low on the crossing"
  (source 5–10, `worldStart: 1`) both show the boat crossing. A shot can
  also sit apart from any event.
- A pose is a position, a look target (or a quaternion) and a field of view.
  It may follow the world state; the template's low shot follows the boat.
- A world with no shots has one implicit shot: aligned, covering the
  whole duration. It still draws an authored pose when no view is given.
- Shot edits never touch layers 1 and 2. World edits show in every shot.

### 4. The free view

When `view` is present, evaluate the world at `worldTime` (fall back to
`sourceTime`) and draw from exactly the view:

```ts
evaluateWorld(world, worldTime ?? sourceTime);
camera.position.fromArray(view.position);
camera.quaternion.fromArray(view.quaternion);
camera.fov = view.fov;
camera.updateProjectionMatrix();
```

Do not run the shot lookup in this path. Anything that belongs to a shot,
such as a caption, a title, a vignette or a camera-attached light, is off:
the explorer shows the world, not the shot.

## Make it hold up from any viewpoint

The free view can stand anywhere inside the bounds and look in any direction,
including straight up and straight down. Check each of these:

- **Sky or horizon in every direction.** Use a sky dome (a large `BackSide`
  sphere with `fog: false` and `depthWrite: false`, drawn first) and move it
  to the camera position every frame so it is always out of reach. Keep the
  camera's far plane beyond the dome radius. A plain `scene.background` colour
  is not a horizon.
- **Fog at the edges.** Make the fog colour the sky's horizon colour, so
  distant ground, hills and sky meet without a seam. Let the fog close in
  before anything ends.
- **Ground everywhere.** Ground covers the whole area inside the bounds and
  continues past the point where the fog is opaque, with no holes and no
  visible edge. Put a horizon (hills, a tree line, buildings) outside the
  bounds in every direction, half lost in fog.
- **Keep the view above ground.** The host clamps the view to the bounds, but
  it knows nothing about terrain. Set `bounds.min[1]` above the highest ground
  inside the bounds (plus the near plane). Keep terrain gentle inside the
  bounds and put the drama outside them. Do not push the view up yourself in
  `render`: the picture would then disagree with the explorer's position.
- **No unfinished backsides.** A single-sided plane vanishes from behind, so
  use `DoubleSide` or a closed mesh (the template's sail). Do not use
  billboard cut-outs or backdrop cards that only work from the front. Finish
  the undersides you can see from `bounds.min[1]`, and any interior the view
  can enter.
- **Lighting from every side.** Do not rig a light to one shot. Light the
  world, and let the shots find it.
- **A good start.** `explore.start` is a composed first view: inside the
  bounds, clear of objects, looking at the world's subject.
- **Bounds that fit the world.** Make them generous enough to wander in and
  tight enough that everything reachable is finished.

To test, render views from each bounds corner looking outward, straight up,
straight down, along `bounds.min[1]` across the ground, and from behind every
subject.

## Performance in software GL

The final video renders in headless Chromium with software WebGL
(SwiftShader). Each frame must complete before the frame deadline (30 s in
the scene host, 35 s for Remotion's per-frame delay), and a long video renders
thousands of frames. Preview and the explorer run on the user's GPU, but the
explorer draws continuously while it is visible. Design for the software path:

- Keep `renderer.setPixelRatio(1)` and size the drawing buffer to the
  requested `width` and `height`.
- Leave shadow maps off. Use baked or painted contact shadows, vertex colours
  or simple dark decals instead.
- Use few lights: a hemisphere light and one directional light go a long way.
  Each point light adds cost to every lit pixel.
- Prefer cheap materials (`MeshLambertMaterial`, `MeshBasicMaterial`, or
  `MeshStandardMaterial` with `flatShading`). Lighting and fog evaluated per
  vertex cost much less than per pixel in software GL; the built-in Lantern
  Valley sample does both per vertex.
- Instance repeated things (`InstancedMesh`), share geometries and materials,
  and merge static geometry. Draw calls cost.
- Avoid large transparent overdraw (layered particles, big alpha planes),
  full-screen post-processing chains and large offscreen render targets.
- Allocate nothing per frame. Rewrite buffers in place and set `needsUpdate`.
- Measure. The template draws a 1280×720 frame in roughly 10–30 ms under
  SwiftShader. A world that takes seconds per frame makes a long video take
  hours.

## The host's clock and determinism

The host owns time. For a clip at composition time `at`, with source trim
`from` and positive playback `speed`, the current source time is:

```text
sourceTime = (from ?? 0) + max(0, compositionTime - at) * (speed ?? 1)
```

The active interval is half-open. A clip with `from`/`to` lasts
`(to - from) / speed`; `hold` lasts `hold / speed`. The same mapping applies
when Astrid materialises a nonzero render window. Two clips can reference the
same package revision while keeping independent `at`, source range and rate.
Use ordinary timeline operations for placement, trim, repetition and rate.
Use the world's source for events and shots.

The same `sourceTime` must give the same pixels, and so must the same
`worldTime` and `view`, whatever was drawn before. A render request outside
`0..duration` fails the scene host. The explorer's world clock loops over
`0..duration`, so world state must be defined across that whole range,
including the end. Changing the duration never stretches or ripples existing
clips or audio clips; reconcile them explicitly.

## Hosted lifecycle

`initialize()` builds the world and awaits its own asset, font and frame
readiness. The bridge then waits for `document.fonts.ready`, decodes document
images, reads the declarations, and acknowledges `initialized`. The host
repeats its initialize message every 100 ms until acknowledged and reports an
error after 30 seconds. Do not use a sleep as a readiness proof.

`render()` evaluates and draws the requested frame. It returns only after the
final visible surface is complete, including any overlay, text or
post-processing; the template calls the WebGL context's `finish()` before
returning. Export therefore captures the requested frame, not a last-good
preview frame. The bridge serialises render requests and uses revision,
instance and request identity to reject stale messages. Preview skips frames
that a newer request has superseded, and exports wait for each exact frame. A
thrown error fails the surface.

`dispose()` releases GPU resources and must be idempotent. The bridge also
calls it on `pagehide`.

## Package and entry contract

The manifest has exactly these fields:

```json
{
  "formatVersion": 1,
  "entry": "world.html",
  "duration": 10,
  "authoredFps": 30
}
```

`entry` is a safe `.html` path without a `..` segment; `duration` and
`authoredFps` are finite positive numbers. Any other manifest field is
rejected. Authoring-side package input is `{manifest, entry: {bytes,
mediaType: "text/html", filename}, assets}`, and the validator requires
`entry.filename === manifest.entry`. It ingests the entry and assets through
the public project-object capability, then serialises one package object
(`application/json`, filename `scene.package.json`):

```json
{
  "manifest": {"formatVersion": 1, "entry": "world.html", "duration": 10, "authoredFps": 30},
  "entry": {"object_id": "…", "digest": "sha256:…", "media_type": "text/html", "size": 1234, "filename": "world.html"},
  "assets": []
}
```

The exact UTF-8 bytes of that body determine the package revision. The
revision is kept outside the body, because including it would be circular.
The only inline clip cache envelope is:

```json
{
  "revision": "sha256:<packageBody-digest>",
  "source": {"objectId": "<package-object-id>", "revision": "sha256:<packageBody-digest>"},
  "packageBody": "<exact UTF-8 JSON text>",
  "html": "<entry object's UTF-8 text>"
}
```

`html` is a derived mirror, not a second source of truth. Its UTF-8 digest and
byte size must match the `entry` metadata in `packageBody`, and the package
revision and `source.revision` must match the digest of `packageBody`.
Manifest-only changes therefore publish a new package revision. Do not spread
another mutable `manifest`, `entry` or `assets` object beside this envelope.

Limits: an entry of at most 8,388,608 UTF-8 bytes, a package body of at most
65,536 characters (the 64 KB limit: it counts only the small JSON `manifest`,
`entry` metadata and `assets` list shown above, not the HTML, so a normal
world is a few hundred characters and a multi-megabyte `.scene.json` file is
fine), at most 16 assets and at most 16,777,216 total asset bytes.
The prepared import below accepts only `"assets": []`, and listing an asset
object does not create a browser URL for the entry. Bundle code, fonts,
textures and models into the HTML. The implementation is in
[`ui/threejs-live-editor/authoring.ts`](../../ui/threejs-live-editor/authoring.ts),
[`ui/threejs-live-editor/runtime.ts`](../../ui/threejs-live-editor/runtime.ts)
and [`shared/live_scenes/package.py`](../../shared/live_scenes/package.py).

## Bring a world into a project

Run every command below **from the repository root**. First type-check the
source (optional but quick), then bundle and package it with one command:

```bash
remotion/node_modules/.bin/tsc --noEmit --strict --skipLibCheck \
  --target ES2022 --module ESNext --moduleResolution Bundler \
  --baseUrl remotion --typeRoots remotion/node_modules/@types \
  astrid/packs/rendering/docs/templates/world-template.ts

mkdir -p output/worlds
node astrid/packs/rendering/worlds/lantern-valley/build.mjs \
  --src astrid/packs/rendering/docs/templates/world-template.ts \
  --out output/worlds/world-template.scene.json
```

`build.mjs` needs `three` and `esbuild` together in one `node_modules` folder:
`remotion/node_modules` (the repository-pinned `three`; run `npm install` in
`remotion` if it is missing), or any other folder you name with
`LANTERN_VALLEY_DEPS=/abs/path/to/node_modules`. It bundles the source, wraps the
bundle in the HTML entry below, computes the entry's SHA-256 digest and UTF-8
size, and writes the prepared package. Options: `--name "My World"` (the page
title; defaults to the file name), `--duration <s>`, `--fps <n>` (default 30)
and `--minify`. It prints the output path, duration, entry size and digest.
`--src` and `--out` take relative or absolute paths, and `--out` creates its
folders, so `mkdir` is optional. `output/worlds/` is just a scratch folder.
Failures (missing dependency, duration mismatch, a missing `</body>`, an
entry over 8 MiB) stop the build with a one-line error and write nothing.
The `packageChars` it prints is the length of the whole file, which is not
the 64 KB package-body limit described under "Package and entry contract" above.

**`DURATION` must equal `manifest.duration`.** Declare it once in the source as
`const DURATION = 10;`; `build.mjs` reads it from there and writes the same
number into the manifest. `--duration` is needed only when the source has no such line; if you
pass it as well, it must match the source or the build stops. Every time and clip range is measured against it.

The entry is `<!doctype html><html><head><meta charset="utf-8"></head><body>
<script>…bundle…</script></body></html>`. It needs both a `<head>` and a
`</body>` tag, because the host injects its CSP into the head and its bridge
before `</body>`; the build checks this. The bundle stays unminified unless you
pass `--minify`, so use that only if you are near the 8 MiB entry limit. The
agent route edits exact text spans, and readable `EVENTS`, `SHOTS` and
`EXPLORE` blocks with unique names can be found and changed in one step.

The prepared handoff is JSON with the exact manifest, entry
`{digest, media_type: "text/html", size, filename}` (SHA-256 `digest` and
UTF-8 byte `size` of the HTML), `"assets": []` and the `html` text. Do not
add an object ID or revision; Runtime assigns both during admission. Two
public routes take it:

- **World Explorer → Import world…** adds the world to the project's library
  without placing it on the timeline.
- **The `Import prepared scene` command** calls
  `parsePreparedScenePackage(bytes)` and `importPreparedScene(ctx, prepared,
  placements)`. These verify UTF-8, JSON, the supported manifest keys, the
  entry filename and media type, the digest and the byte size. The command
  places one ordinary visual clip on `V1` at `0`, source `0..duration`, rate
  `1`, and awaits the timeline flush. Repeating it chooses the next free
  `live-scene-import-N` clip ID. Callers that need different timing pass
  their own validated placements. Read active clip IDs and timing from the
  selected public timeline. The aliases `parsePreparedMaplePackage` and
  `importPreparedMapleScene` remain only for existing Maple acceptance
  callers.

This is bounded prepared-HTML admission. It is not ZIP import, a runtime
bundler, dependency installation, remote module loading or private-store
injection.

## Edit a world through the agent route

The registered public handler is the `astrid.live-scene-request/v1` ACP route,
not a private editor store or an Astrid CLI verb. The SDK types are exported
from Astrid App's public SDK, and the pack binds the handler through
`ctx.liveSceneAuthoring`. The agent receives an exact
`<astrid_live_scene_context>` scope (`astrid.live-scene-context/v1`). It reads
a bounded excerpt first, then publishes against the same capture:

```text
<astrid_live_scene_request>{
  "schema": "astrid.live-scene-request/v1",
  "requestId": "read-1",
  "scope": {"sessionId":"…","turnId":"…","projectId":"…","timelineId":"…","capturedTimelineVersion":7},
  "placementIds": ["<placement-id-from-active-timeline>"],
  "action": "read", "find": "const SHOTS", "offset": 0, "length": 2000
}</astrid_live_scene_request>
```

The placement ID above is illustrative. Use the IDs from the selected
timeline's current public context.

- **Read**: `offset >= 0` and `length` of 1–8,192 UTF-16 characters. The
  optional `find` (at most 512 characters) must match exactly once in the
  entry; `offset` then counts back from the match. The result returns the
  excerpt, the total source length, placement timing, and a `capture` with
  the project, timeline and version plus package and entry identities.
- **Publish**: copy that capture exactly and send 1–16 `{before, after}`
  replacements, each at most 8,192 characters. Each `before` must occur
  exactly once and come from an excerpt already read. Missing, ambiguous,
  overlapping, duplicate, stale or out-of-scope edits fail closed.
- A request is at most 32,768 UTF-8 bytes and a result at most 24,576. Never
  send a multi-megabyte entry through ACP.

Publication preserves the captured manifest, so it cannot change the
duration. The pack re-reads and digest-checks the package and entry, ingests a
new immutable revision, updates every selected placement in one atomic patch,
awaits `TimelineOps.flush()`, and returns a small durable receipt. The
capture's `projectId`, `timelineId` and `capturedTimelineVersion` are
expected-head guards. On a stale scope, discard the candidate, re-read, and
retry from a new capture. `flushReceipt.version` is the editor timeline
version, not the package revision.

Know which layer an edit touches:

- **Shot edit**: change a shot's `start`, `end`, `worldStart` or pose.
  The world and its events are unchanged. Check source times on both sides of
  each changed boundary.
- **Event edit**: change the event object that both the declaration and the
  behaviour read. Every shot that shows that world time changes with it.
  Move a shot's `worldStart` if it should keep framing the event.
- **World edit**: change construction or `evaluateWorld`. Every shot, and
  every view, shows the change. Re-check "Make it hold up from any
  viewpoint" above.
- **Duration change**: a manifest change plus explicit clip reconciliation
  through the timeline APIs. Publication never stretches or ripples clips or
  audio.

After publishing, open the world in the World Explorer. Explore it, check the
shots and events the host kept, and give the user the editor link. Render the
pinned revision only if the user asks.

## Audio, security and export boundaries

A hosted world is silent. It must not start `Audio` or WebAudio, or use audio
as its clock. Audible content uses ordinary timeline audio clips, which own
placement, trim, rate, mute and gain. There is no automatic audio extraction,
procedural capture or scene-audio synchronisation. See the
[render capability contract](../../actions/render/STAGE.md) for the render
facade and audio ownership.

The preview document is an iframe with `sandbox="allow-scripts"` and an
opaque origin, plus this CSP: `default-src 'none'; script-src
'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src
data:; connect-src 'none'; media-src 'none'`. There is no network: inline
scripts and styles, data/blob images and data fonts only. This limits
transport and network access, but it is not an arbitrary-code security
sandbox. Do not describe it as a security boundary or load credentials,
remote dependencies, project URLs or unverified code.

Astrid's Three.js/Remotion composition consumes the pinned revision and waits
for each requested frame. When you call the managed `rendering.render`
executor directly, pass `selector: "rendering.threejs"` for a timeline that
contains `haus.astrid.liveScene`. An omitted selector defaults to ordinary
`rendering.remotion`, which does not implement this composition. The Astrid
App editor router already selects the Three.js backend. Do not add this clip
type to ordinary Remotion's registry. The composition mapping is in
[`ThreeTimelineComposition.tsx`](../../../../../remotion/src/ThreeTimelineComposition.tsx).
The final video never sends a `view`.

## Worlds that are not explorable yet

Live scenes made before this contract may not declare `explore`. The World
Explorer opens them in Shots mode only, and their timeline preview and video
are unchanged. Make them explorable when you next edit them.

Maple Hollow, prepared by
[`shared/live_scenes/maple.py`](../../shared/live_scenes/maple.py), is one
such world: a continuous take whose Journey evaluates the world and the camera
together from source time. Its native camera and feedback controls are removed
in hosted mode. To make it explorable, change the adapter, not the world.
Declare `explore` (start at the journey's opening pose, with bounds around the
hollow). Declare its caption moments as events. In `render`, when a `view` is
present, evaluate the journey at `worldTime`, then set the camera's position,
quaternion and field of view from the view and hide the caption. Keep its one
implicit shot; do not split the take into shots for their own sake. The
Maple camera-edit helper is an acceptance fixture, not a general camera
editor.

## Check a world

Before calling a world done (items 2 to 5 render frames: run them only when the
user asks for frames or a render):

1. The declarations the host kept (`events`, `shots`, `explore`) match what
   you wrote. The explorer shows them, and so does `SceneFrameHost.info`.
2. Frames either side of every shot boundary show the right shot at the
   right world time.
3. Rendering a source time again after later frames gives identical pixels.
4. Views from the explore start, each bounds corner looking outward, straight
   up, straight down, low across the ground, and behind every subject all
   hold up.
5. A frame at the output size renders quickly under SwiftShader.

A developer-only host check loads the prepared JSON through `sceneDocument`
and `SceneFrameHost` in headless Chromium with
`--use-angle=swiftshader --enable-unsafe-swiftshader`, renders the frames
above with and without `view`/`worldTime`, compares the repeat, and times
each frame. That internal smoke does not replace visible acceptance through
the normally activated extension and public admission path. If the real
browser host is unavailable, report that separately from typecheck and bundle
results.
