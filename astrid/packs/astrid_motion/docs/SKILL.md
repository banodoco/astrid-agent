---
name: astrid-motion
description: Use reusable Astrid Motion rendering elements for pixel motion, overlays, and transitions.
---

# Astrid Motion Pack

Use this pack when a timeline needs a pixel-motion building block for the
"Pixel Modernism" explainer look: pixel plates that pan and zoom on whole pixels,
transparent pixel cutouts that step, editorial type, floating callouts, and
hard pixel-block wipes.

Every element is a render-time overlay (`clipType` = the element id). It draws
inside the Remotion composition and does not generate pixels, import media, or
render the video. Use the rendering pack to render, and the pixel pack to
snap or cut out art.

Element ids are `am-*` so they never collide with the shadowed `text-card`
pair. Keep the motion grammar consistent: pixel positions snap to the 6 px
logical grid, stepped elements change on two- or three-frame boundaries, type
may ease, and cuts stay hard.

## Rules that apply to every element

1. **Managed media goes on `clip.asset`, not in `params`.** Import the PNG with
   `python3 -m astrid media import <file.png> --project <p> --json`, add a
   registry entry (`media_id` from the import, `content_sha256` = its
   `sha256:` digest, `type`, `resolution` as `WxH`), and set the clip's
   `asset` to that registry key. The composition hands the entry to the element
   as `assetEntry`, and the render backend rewrites its `file` to a loopback
   URL. `params.src` only takes a static http(s) URL or public path. A registry
   key placed in `params` is not resolved.
2. **Units.** Pixel elements use logical px: 320x180 space, one logical px is 6
   screen px at canvas 1920x1080. `am-snap-plate` focus and pan, and
   `am-sprite` x and y, are logical px. `am-type` and `am-callout` use canvas
   px. `am-sprite` scale is screen px per art px (6 = one art px per logical
   px).
3. **Timing.** Clip `hold` (seconds) sets length. Frames come from the
   composition fps. Stepped motion uses `stepFrames` (2 or 3). Type eases.
4. **Determinism.** No `Math.random`. Seeded orders use a hash of (seed, index).
5. **Overlays on top.** Visual tracks draw earlier array entries on top. Put
   overlay tracks (`fx`, `callouts`) before the picture or plate track.
6. Element `params` are not schema-checked at validation time. Keep to the
   documented shape. Out-of-range values are clamped by the component.

## am-snap-plate: pixel plate with stepped pan and zoom

Shows a pixel snapshot (1920x1080, or any 320x180 multiple) through an integer
zoom of 1 to 4.

| param | default | meaning |
|---|---|---|
| `zoom` | 1 | integer 1-4. At zoom 1 the plate fills the frame and pan has no effect. |
| `focus` | `{x:160,y:90}` | logical px placed at the frame centre. Clamped to the plate. |
| `pan` | `{dx:0,dy:0}` | logical px moved per step. Positive dx moves the view right. |
| `stepFrames` | 2 | 2 or 3. Pan changes only on step boundaries. |
| `tint` | none | `{color, opacity}` flat overlay, default opacity 0.25. |
| `enter` | `cut` | `cut` or `blockWipe` (reveal over `enterFrames`). |
| `exit` | `cut` | `cut` or `blockWipe` (cover over the last `exitFrames`). |
| `enterFrames` / `exitFrames` | 6 | block-wipe length. |
| `pattern` / `seed` | diagonal / 1 | block order for the wipes: diagonal, random, or scan. |
| `wipeColor` | `#1F1F1F` | block colour. |
| `background` | `#F7F4ED` | colour behind the plate. |
| `src` | none | static URL or public path (use `clip.asset` for managed media). |

For a flat coloured background with no image, `am-snap-plate` needs no asset: set `params.fill` to a brand token (`paper`, `charcoal`, `rust`, ...) or a CSS colour such as `#F4D2B2`. Do not generate a placeholder PNG for it.

```json
{"id":"plate-01","at":0,"track":"base","clipType":"am-snap-plate","asset":"plate-p01","hold":4,"params":{"zoom":3,"focus":{"x":120,"y":60},"pan":{"dx":2,"dy":0},"stepFrames":2,"enter":"blockWipe","enterFrames":6,"exit":"cut"}}
```

## am-sprite: transparent pixel cutout with stepped motion

A transparent cutout (managed PNG on `clip.asset`) placed on the logical grid.

| param | default | meaning |
|---|---|---|
| `scale` | 6 | integer screen px per art px. 6 gives one art px per logical px. |
| `x`, `y` | 0, 0 | logical px of the sprite's top-left corner. |
| `flipX` | false | mirror the sprite in place. |
| `keyframes` | none | `[{frame, x, y, flipX?}]` held poses. The last keyframe at or before each step is active. No interpolation. |
| `stepFrames` | 2 | 2 or 3. Keyframe changes land on step boundaries. |
| `enter` | `cut` | `stamp` (frame 0 on at 1 px high, frame 1 off, frame 2 on at rest), `slideIn` (closes `slideDistance` logical px in 4 steps from `slideFrom`), or `cut`. |
| `frames` | none | horizontal strip `{frameWidth, frameHeight, count, fps}`. Frame index is `floor(frame*fps/clipFps) % count`, so it changes on steps. |
| `blinkAt` | none | clip frame or frames where the sprite hides for two frames, or shows `blinkIndex` when `frames` is set. |
| `src` | none | static URL or public path. |

```json
{"id":"mink-run","at":1.0,"track":"fx","clipType":"am-sprite","asset":"mink-run-strip","hold":3,"params":{"scale":6,"x":40,"y":96,"enter":"stamp","frames":{"frameWidth":32,"frameHeight":32,"count":8,"fps":12},"keyframes":[{"frame":0,"x":40,"y":96},{"frame":45,"x":60,"y":96,"flipX":true}],"blinkAt":[60]}}
```

## am-type: editorial type

| param | default | meaning |
|---|---|---|
| `text` | required | the copy. Empty text renders nothing. |
| `font` | `display` | `display` = Gelasio, `label` = Departure Mono (uppercase, +0.12em). |
| `size` | 96 / 28 | canvas px. |
| `color` | `#25241F` | ink. |
| `align` | `left` | `left`, `center`, or `right`. |
| `x`, `y`, `width` | 96, 96, 1200 | canvas px. |
| `reveal` | `slideUp` | `slideUp` (masked, 8 frames, ease-out), `type` (one char every 2 frames), or `none`. |
| `italicWord` | none | first matching word in Gelasio Italic, rust `#A94714`. |
| `italicColor` | `#A94714` | colour of the italic word. Rust is too dark on charcoal: use orange `#FF7A2E` there. |
| `underlineWord` / `underlineAt` | none / 0 | first matching word gets an orange underline. It draws in over 6 frames from `underlineAt`. |
| `tracking` / `lineHeight` | -0.01 / 0.95 display; 0.12 / 1.3 label | em. |

```json
{"id":"head-1","at":0.5,"track":"fx","clipType":"am-type","hold":3,"params":{"text":"Ship it tomorrow.","italicWord":"tomorrow","font":"display","size":120,"x":96,"y":300,"width":1400,"reveal":"slideUp","underlineWord":"tomorrow","underlineAt":30}}
```

## am-callout: floating card with an orange connector

The site's annotation card: panel white, 1px rule border, soft shadow, Gelasio
heading (weight 650), Inter body. The connector leaves the card's left or right
edge at the title line and draws in on steps to a dot at `anchor`.

| param | default | meaning |
|---|---|---|
| `title`, `body` | none | at least one is required. |
| `x`, `y`, `width` | 1200, 160, 520 | card top-left and width, canvas px. |
| `anchor` | `{x:960,y:540}` | canvas px where the connector ends. |
| `drawFrames` | 12 | frames until the connector finishes. |
| `stepFrames` | 2 | 2 or 3. |

Font note: only Gelasio 400 and 700 are vendored, so weight 650 renders at 700.

```json
{"id":"callout-1","at":2,"track":"callouts","clipType":"am-callout","hold":3,"params":{"title":"9,640 commits","body":"Thirteen repos, one year.","x":1180,"y":200,"width":560,"anchor":{"x":720,"y":430},"drawFrames":12}}
```

## am-pixel-wipe: pixel-block wipe

A full-frame overlay of 6 px blocks. `cover` fills the frame; `reveal` clears
it. It advances one block set per frame over `frames`, then holds.

| param | default | meaning |
|---|---|---|
| `mode` | `cover` | `cover` or `reveal`. |
| `frames` | 12 | frames to finish. |
| `color` | `#1F1F1F` | block colour. |
| `pattern` | `diagonal` | `diagonal` (corner to corner), `random` (seeded hash), or `scan` (left to right). |
| `seed` | 1 | seed for `random`. Same seed, same order. |

```json
{"id":"wipe-out","at":5.5,"track":"fx","clipType":"am-pixel-wipe","hold":0.6,"params":{"mode":"cover","frames":12,"color":"#1F1F1F","pattern":"random","seed":7}}
```

## am-pixel-shape: asset-free pixel marks

Draws one hard-edged pixel overlay without an image asset. `x` and `y` are
logical coordinates on the 320x180 grid; `px_scale` sets screen pixels per
cell. The shapes are a ring, filled dot, bottom underline, right arrow, or
rectangle with optional fill, outline, and stepped corner radius. `blinkAt`
hides the mark for two clip frames at each listed frame.

| param | default | meaning |
|---|---|---|
| `shape` | `ring` | `ring`, `dot`, `underline`, `arrow`, or `rect`. A 3x3 dot is a full stop. |
| `color` | `#1F1F1F` | Ink for ring, dot, underline, or arrow. |
| `px_scale` | 6 | Screen pixels per grid cell. |
| `x`, `y` | 0, 0 | Logical top-left position. |
| `w`, `h` | 24, 24 | Box size in cells. |
| `stroke` | 2 (1 for rect) | Thickness in cells. For a rect it sets the inside outline width. |
| `fill`, `outline` | none | Optional rectangle fill and inside border colors. Omit either to leave it transparent. |
| `radius` | 0 | Rectangle corner radius in cells, stepped to the grid. |
| `blinkAt` | none | Clip frame or frames where the mark hides for two frames. |

```json
{"id":"tool-tile","at":145.733333,"track":"sprite","clipType":"am-pixel-shape","hold":0.933334,"params":{"shape":"rect","fill":"#F7E6CF","outline":"#151311","px_scale":5,"radius":3,"stroke":1,"w":16,"h":16,"x":189,"y":68}}
```

## Media registry entry (for `clip.asset`)

```json
"plate-p01": {"media_id": "<id returned by media import>", "content_sha256": "sha256:<digest>", "type": "image/png", "resolution": "1920x1080"}
```

## Checks

- `python3 -m astrid.core.element.cli list --kind effects | grep am-`
- `python3 -m astrid.core.element.cli inspect effects am-type --json`
- `python3 -m astrid.core.pack.cli validate astrid/packs/astrid_motion`

## Preview loop (stills and mp4, no runtime)

Use this while building or tuning an element. It mounts each element component
directly in a Remotion harness, so you do not need a timeline, a runtime or a
registry alias. Node must be 20.19.4 (Remotion pins it). Put Node on PATH with
`export PATH=~/Developer/astrid-video/tools/node-v20.19.4-darwin-arm64/bin:$PATH`.

```bash
OUT=<scratch dir>
python astrid/packs/astrid_motion/preview/make_art.py $OUT/.public/am   # test art, once per OUT (venv python, needs Pillow)
OUT=$OUT ONLY=type node astrid/packs/astrid_motion/preview/render.mjs  # scene names from scenes.json; mp4 when "mp4": true
```

Output is `$OUT/<scene>-f<frame>.png`. Read a PNG to look at it. The first run
bundles in about 10 s, and later runs bundle in about 3 s. Add a scene by
editing `preview/scenes.json`: `{"name", "frames": [..], "mp4": false,
"assets": {"key": {"file": "am/x.png", "type": "image/png", "resolution": "WxH"}},
"layers": [{"element": "am-type", "at": 0, "hold": 3, "params": {...}}]}`.
Asset `file` paths are relative to `$OUT/.public/`. The harness also renders a
`control-plain` layer, a plain `<Img>` with no pixelated style, for comparison.
The fonts come from `remotion/src/fonts.ts` (`FontProvider`), so previews
match the render.

## Known limits

- **Render alias (blocks timeline renders).** `scripts/gen_effect_registry.py`
  imports pack elements as `@pack-astrid_motion-elements-effects/<id>/component`.
  `remotion/remotion.config.ts` and `remotion/webpack-alias.mjs` alias only the
  `local` and `rendering` packs (`remotion.config.ts:67-68` for effects). Any other pack needs
  `ASTRID_PACKS_PATH` to add its alias. So a timeline that uses `am-*` clips will
  not bundle until the config aliases in-tree pack element roots. Use the preview
  loop for stills and mp4 until that change lands.
- `am-snap-plate` zoom is fixed per clip. For a stepped zoom punch, split the
  plate into two clips.
- `am-sprite` `slideIn` moves from the rest position by `slideDistance`, not from
  the canvas edge.
- Only Gelasio 400 and 700 are vendored, so `am-callout` weight 650 renders at 700.

## am-presenter: animated pixel presenter overlay

Draws the speaker's mouth, blinks, a head bob and an optional zoom punch on top
of a presenter plate, plus a placeholder slate. Put its track earlier in the
clip array than the plate, so it draws on top (rule 5). It is not a plate and
does not replace one: the plate is an `am-snap-plate` clip on the track below.

- `zoom` 1|2|3 and `focus` {x, y}: the same view as `am-snap-plate`. The focus
  lands on the frame centre after clamping, so both layers stay registered.
- `mouth` {x, y, w}: top-left and width of the closed mouth line on the native
  plate. `skin` erases it, `lip` is the dark opening, `inner` the interior line.
- `words` [[start_s, end_s], ...]: seconds from clip start (Edge TTS word
  boundaries). Inside a word the mouth cycles closed, half, open on a 3-frame
  cycle, offset per word by the seed. A gap of 0.12 s or less holds the last
  state. A longer gap closes the mouth. Pixel states only, no interpolation.
- `eyes` [{x, y, w}] (top row of each eye), `blinkEvery` seconds (30% seeded
  jitter), `noBlink` [[s, e]] spans where a blink may not start, `seed`.
- `bob` 0|1: a 1 px downward nudge on the first frame of every 3rd word.
- `punchAt` [frames]: zoom +1 (capped at 4) for 6 frames.
- `label` (default `PLACEHOLDER · POM ON CAMERA · TAKE 03`), `timecodeStart`
  (HH:MM:SS:FF, default 01:02:14:00; REC timecode runs from it), `chip`
  {text, swapTo?, swapAt?}: the text swaps on frame `swapAt`, with a 2-frame
  orange flash.

**Punch and bob must match on the plate.** Both are view changes, so the plate
must apply them too. Give the presenter plate the same `zoom`, `focus`,
`punchAt`, `words`, `seed` and `bob`, with `pan` 0. Then both use
`presenterView()` in `am-presenter/presenter-core.ts`. As of this commit,
`am-snap-plate` does not read `punchAt` or `bob`, so punches and bobs drift
until it does. Its focus and zoom geometry already matches `presenterView()`.

Logical px are 320x180 at 6 px per px at zoom 1. Overlay pixels are absolutely
positioned divs, not canvas, so edges stay crisp. The label, REC row and chip use
Departure Mono, and the crop marks are inset 48 px.

## am-discord: pixel-styled Discord channel reconstruction

A reconstruction, not a screenshot. It has no Discord logo or wordmark. The
window is 1280 px wide and centred. Its height is `frame.height`, or with
`fit: "content"` it hugs the visible stack (never below `minHeight`, default
360). The window snaps taller as messages land. Message text is Inter (Noto
Sans is not shipped), names are Inter bold, and timestamps and system lines are
Departure Mono. Palette values come from the lore notes (04-lore.md B3) and are
[S] or [U] there. Check them against a real client before sign-off.

- `channel`, `server`, `channels`: header and sidebar text, verbatim.
  `sidebar: false` hides the channel list and widens the chat.
- `messages` [{author, tag, avatarColor, time, lines, appearAt, typeOn}]: one
  message per post. Adjacent messages from the same author, tag and time render
  as one block, with one header and then the lines. `lines` are paragraphs,
  shown verbatim. `@everyone` is a mention pill. `{u:word}` is a static
  underline and marks the word as a target. `time` is shown as given. With
  `typeOn`, characters reveal on `typeStepFrames` (default 2) steps.
- `dateDividers` [{label, at, jump?}]: after the first divider, the date flips
  split-flap through the days between the two dates over `jumpFrames` (default
  12, ease-out), then lands. `jump: false` lands at once.
- `reactions` [{emoji, countFrom, countTo, startAt, stepFrames, onMessage?,
  mine?}]: a pill with a stamp entrance (0.6, 1.1, 1.0), counting in
  `stepFrames`. Attaches to `onMessage`, or else the latest message posted by
  `startAt`. Emoji is Unicode, or a path or URL for a pixel icon.
- `joins` [{name, at, time?}]: system lines with a green pixel arrow, stacking
  upward. Accelerate them by shrinking the gaps between `at` values.
- Decorations take `{word, at, onMessage?}`. The first match is used unless
  `onMessage` is given. Each one steps on whole frames:
  - `underline` [{color}]: draws in over 6 frames, one step per frame.
  - `strike`: rust bar on frame `at`, orange bar one frame later, offset 3 px.
  - `highlight` [{color: '#ED6B23', alpha: 0.45}]: a Vox-style marker bar behind
    the word. It is cap height plus about 3 px either side, with 2 px stepped
    ragged ends, and wipes across in 4 stepped frames. `underline` stays separate.
  - `circle`: a rust (#A94714) hand-drawn ellipse, drawn in 6 stepped segments.
    The shape is seeded from the word, so it is repeatable.
  - `annotation` [{text}]: a Departure Mono callout card above the word, with an
    orange connector that grows in 2 steps. The card lands on `at + 2`. Its look
    is taken from am-callout. Headroom is added above the stack when any
    annotation is present.
- `camera` [{at, zoom: 1|2|3, focus: {x, y}}]: stepped integer push-ins of the
  whole window. Each step snaps on its frame, with no easing. `focus` is in
  window px, measured from the window's top-left at its current size, and lands
  on the frame centre. Use it to punch into a word, and read the focus from a
  zoom-1 still first.
- `badge` (default `RECONSTRUCTION · REACTIONS & JOINS ILLUSTRATIVE`, empty to
  hide) sits at the frame's bottom-right. It does not move with the camera.
  `frame` {width, height, radius, outline, shadow} sets the window.

Messages, joins and dividers interleave by `at`, newest at the bottom. The
window keeps its width and centre. The top fades only in frame mode. Keep joins
and reactions labelled illustrative (the badge).

Use the pack's Remotion preview scenes in `preview/scenes.json` as visual
references. Project exploration stills are kept outside this reusable pack.

## am-flap: split-flap readout

Dark split-flap tiles (`#25241F`) with a 1 px split line and Departure Mono
glyphs. Each tile flips through the drum on two-frame steps (`stepFrames`), and
tiles start one `stagger` apart, left to right. Digit-to-digit flips run round
the digit ring (at most nine steps), so counters stay readable.

- `values` [{text, at}]: keyframed targets. `at` is the clip frame when the flip
  toward `text` begins. Texts are upper-cased and padded to the longest one.
  The first flip starts from blank tiles.
- `tileW`, `tileH` (snap to 6, default 60x84), `gap` (6), `color`, `tileColor`,
  `accentColor` (`#ED6B23`), `accentIndex` (0-based character positions painted
  orange).
- `x`, `y` (top-left of the strip, default 96, 96), `align` (`left`, `center`,
  `right`: which edge or centre sits at x), `label` (mono caption above),
  `labelColor`.
- `stagger` (frames, default 2), `stepFrames` (default 2).

Example: `{"values": [{"text": "26.10.25", "at": 0}, {"text": "09.10.26", "at": 90}], "label": "DAYS SINCE 'TOMORROW'", "accentIndex": [0, 1]}`.

The drum logic is exported from `am-flap/flap.tsx` (`flapChars`, `flapGlyph`,
`FlapStrip`) and is reused by `am-churn`.

## am-churn: the ledger of obsession (data-driven)

A full-frame data passage on the editorial grid (12 columns, 96 px margins).
Paper by default, `variant: "dark"` for night frames. Everything comes from
the action's explicit params; it does not read project history or local files.
Supply daily rows, highlights, totals, and timeline keys from the caller. The
project-specific history source and parameter-preparation script are not part
of this reusable pack.

- `rows` [{date (ISO), added, deleted, commits?}]: one row per calendar day,
  ascending, contiguous. Cumulative sums are computed inside the element.
- `highlights` [{date, repo, sha (7 chars), subject, added, deleted}]:
  chronological. `subject` is the real commit subject. Rows on the same day
  stamp together.
- `totals` {commits, added, deleted}: the last row's cumulative values are forced
  to these, so the counters end exactly on them at `endDate`.
- `startDate`, `endDate` (informational; the last `timeline` key must be
  `endDate`).
- `timeline` [{atFrame, date}]: keyframes, linear between keys, dates floored for
  display. Repeat a date to hold, and bunch frames to accelerate. The default
  crawls Oct to Dec, accelerates through spring, then runs to 09.10.26 at clip
  frame 480 (plus any freeze hold).
- `freezeAt` {atFrame, highlight (sha prefix), holdFrames (default 90),
  cardDelay (default 8), label (default `THE RENAME`), date?}: the timeline
  holds on the highlight's day while a spotlight card (`#FFFEFA`, 2 px ink
  outline, orange connector with dot ends) draws in next to its row. The
  timeline resumes after the hold. `null` disables it.
- `footnote` (mono, bottom-left). `binDays` (days per heartbeat bar, default 11,
  giving 34 bars). `scrollFrames` (stamp list scroll, default 14).

What is drawn: a split-flap date readout (`ENTRY DATE`, with `DAY n OF N`), a
timeline track with month ticks, the two counters (`LINES WRITTEN` in ink,
`LINES DELETED` in rust, a pixel sign, and the digits that changed in the last
frame tick orange, limited to the three least significant cells), the commit
count, the stamp column (date, subject, `repo · sha · +N −M`), and the
heartbeat (added up in ink, deleted down in rust, a common sqrt scale in 6 px
cells, the current bin marked orange).

## am-ui-sketch: a window that changes while you use it

A ruled app window drawn only with shapes (2 px ink outline, sand title bar,
three pixel dots). Three layers swap independently. Missing layer values carry
forward. The window is 960x636 and the prompt bar sits 24 px below it, so the
group is 756 px tall: exactly 70% of a 1080 frame.

- `states` [{at, interface?, behaviour?, data?}] with values `A`, `B` or `C`.
  - INTERFACE: A is a left sidebar with a big `RUN` button, B a right sidebar
    with a slider, C top tabs with toggles.
  - BEHAVIOUR: A a linear node chain, B a routed graph (router and two paths),
    C a loop (check feeds back to the model).
  - DATA: A a table, B the same table with its columns reshuffled, C three cards
    with a bar each.
- A change flickers the changed region in 6 px blocks (density 35% then 20%) on
  two frames, then swaps in. A burst of orange 6 px sparks steps out from the
  region centre over three steps.
- `cursor` {at, path: [{x, y, frame}], holdFrames (18), stepFrames (2)}: a chunky
  pixel arrow (6 px cells). It holds on each path point and steps toward the
  next every `stepFrames`, snapped to 6 px. Path points are window px (the
  960 px window, border included) at absolute clip frames. Place each point on
  the region that changes next, arriving about 8 to 14 frames before the swap.
  It hides `holdFrames` after the last point. `null` or omitted: no cursor.
- `prompt` {text, at}: a chat bubble below the window types in at two frames a
  character with a caret, then a `LLM` chip follows. `null` or omitted hides it.
- `allAt`: at that frame every layer mutates once every two frames for twelve
  frames, with a spark each step. Then the states resume.
- Placement: `x`, `y` (group top-left in frame px; default centred in
  `width` x `height`, which default to 1920x1080). `scale` (1 or 2, integer;
  default the one closest to 70% of the frame height, which is 1 at 1080).
  At scale 2 the group is 1920x1512, so it needs a frame at least that tall or
  an explicit `y`. `title` (default `untitled app`), `seed`.

Keep layer changes at least six frames apart so each flicker reads.

## am-burst: pop-up burst

Items stamp in on an arc around `center` (`x`, `y`). Each item appears with a
two-frame flicker (hidden, shown, then solid), holds with a one-logical-px bob
on threes (`bob`, default 6 px), and throws a ring of 6 px squares that expands
in three steps behind it (radius 2, 4 and 6 cells, drawn as dots on a 12 px
pitch). Positions snap to the 6 px grid.

- `items` [{src | glyph, label?, angle (degrees, 0 right, -90 up), distance (px),
  at (frame, default index times `stepFrames`), size (multiple of 6, default 96)}].
  `src` is a pixel cutout (static URL, public path, or data URI) drawn with
  pixelated scaling. `glyph` is short text in Departure Mono. Without either, an
  ink placeholder square is drawn. `label` is a mono caption under the item.
- `stepFrames` (default 4), `ring` (default true), `color` (ink default).
- `confetti` {at, count (default 48), seed, spread, life (default 48), colors?}
  or `null`: seeded 6 px squares in palette colours, fanning out and falling in
  steps of two frames.
- `seed`, `x`, `y`, `width` (1920), `height` (1080).

Use it for the Lindgren aside (book icons on an arc) and celebration beats.

## v5 additions: real-footage slots and the pixelate move

**Pixelate (mosaic) transition.** `am-snap-plate` and `am-footage` take
`mosaicIn` / `mosaicOut` `{frames (8), from|to (48), steps?}`: the picture
averages into 6, 12, 24 and 48 px blocks on hard steps (6 px is the 320x180
grid). Put `mosaicOut` on the last frames before a cut and `mosaicIn` on the
first frames after it: real footage dissolves into the pixel world and back.
`am-presenter` takes the same ramps and hides its face pixels while the plate
is a mosaic, and its `chrome` (`full`, `label`, `none`) slims the placeholder
slate to one small label.

**am-footage: a real-footage slot.** A filmed shot on `clip.asset` (video via
OffthreadVideo, or a still), cover-fitted; `zoom` (>= 1) and `focus` (source
fractions) crop it, `push.to` zooms slowly over the clip, `volume` defaults to 0
(the VO track carries the voice). `faceZone` `{x, y, w, h}` and `handMark`
`{x, y, size}` (canvas px) declare where the face and the palm sit, so overlays
avoid the face and composites land on the palm. With no asset it draws the slot
card (slot id, framing note, dashed face zone, dashed hand outline and palm
mark), so the edit reads before anything is filmed. `inset` `{x, y, w, h, radius, shadow, border}` (canvas px) frames the
picture on the `background` field instead of filling the frame: a real screenshot (the Discord post, a repo
page, the app) shown as a document on the page, with zoom/focus/push/mosaic working inside the rect and the
label hanging under it. A plain field with nothing on it is an `am-snap-plate` of a flat 320x180 PNG.

**am-tweet.** A reconstructed post card: generic silhouette avatar (never a
likeness), `name`, `handle`, `date`, a dashed slot body (default
`[REAL TWEET TEXT + LINK NEEDED]`), `link`, an always-on `RECONSTRUCTION` chip.
Only ever fill `body` with the verbatim post.

**am-quote.** A quotation that reveals on the VO's word onsets (`words`, the
builder's `@words` helper), then `strikeAt` strikes through it word by word and
`stamp` `{text, at, x, y, rotate, size}` slams a rubber stamp. `mark`
`{word, at}` highlights one attribution word.

**am-terminal.** Commands type in after an orange prompt (`kind: cmd`), output
lands whole (`out`, `dim`, `ok`), a `progress` line counts `{n}` and `{eta}`.
`badges` stamp in under the window. Real commands and real output only.

**am-droste.** The plate recurses inside its own `screen` rect (logical px; P-02's
monitor is `{x: 83, y: 38, w: 147, h: 75}`). `moves` `[{at, to, frames}]` step
between levels; the camera zooms about the recursion's fixed point, so level n
lands framed like level 0. `depthTint` darkens deeper levels.

**am-seasons.** A procedural window onto a street through the year (autumn,
winter, spring, summer, autumn2): sky, tree, weather, a growing plant and a pile
of mugs. `sequence` `[{at, season}]`. The stand-in for the S-00..S-04 plates.

**am-orbit.** A small orbit HUD: the Earth steps round the Sun past twelve
month ticks (`fromMonth`, `months`, `frames`), with a month readout.

**am-discord composer.** `composer` `{message, startAt, typeStepFrames, key}`:
that message types into the message box, Enter is pressed on the three frames
before its `appearAt` (the box flashes, an ENTER keycap stamps at `key`), then it
posts. Use the `camera` to push in on the box so the typing is readable.

**am-sprite.** `shadow` `{groundY, w, h, opacity, fade}` draws a stepped contact
shadow on the ground line that narrows as the sprite rises; `frames.start` and
`frames.sequence` pick poses out of a strip.

Preview: `SCENES=<file.json>` renders a scene list kept outside the pack, and
`EXTRA_PUBLIC=name=/abs/dir,...` links read-only art into the harness's public
dir. Every element above is registered in the harness.

## v6 additions: pushes instead of jumps, silhouettes, org charts

- **Slow push.** `am-snap-plate` and `am-presenter` take `push` `{to, frames, at}`: zoom moves linearly from `zoom` to `to`. Give both layers the same value (the builder copies it from the presenter to the plate). Use a push where a beat needs emphasis instead of a hard 2× or 3× jump or a `punchAt`; hard crop changes look cheap on real phone footage. On a real-footage swap it becomes `am-footage` `push.to`.
- **am-sprite `silhouette` / `outline`.** `silhouette: {until}` draws the art as a flat ink shape until that frame (a covered or unrevealed object). `outline: {color, px, pulse}` adds a hard pixel ring that blinks one px wider every `pulse` frames (a pixel glow). Both are filters inside the sprite's scale, so they land on the art grid.
- **am-orgchart.** A recursive org chart: `levels` rows of pixel bots, each managing `branching` more. Rows stamp in at `revealAt[k]` and fill left to right over `spread` frames, with optional row `labels` and a rubber `stamp` `{text, at, x, y, rotate, size}`.
