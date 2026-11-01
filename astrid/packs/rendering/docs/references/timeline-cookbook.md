# Timeline cookbook

Read this reference when authoring or validating a timeline document. The
runtime owns the saved document; these examples describe the renderable JSON
boundary rather than a local storage format.

The canonical end-to-end workflow is the [Astrid timeline skill](../SKILL.md);
the private composed-evidence backend contract is [Timeline Visualize](../../actions/timeline_visualize/STAGE.md).

## Minimal renderable config

Use explicit tracks, explicit clip types, and an output contract:

```json
{"tracks":[{"id":"cards","kind":"visual","label":"Cards"}],"clips":[{"id":"title","at":0,"track":"cards","clipType":"text","hold":2,"text":{"content":"HELLO ASTRID","fontSize":64,"color":"#ffffff","align":"center"}}],"output":{"resolution":"640x360","fps":30,"file":"title.mp4"}}
```

Text-shaped data without `clipType: "text"` is ambiguous and is rejected
before renderer admission. A reusable visual element is a clip whose
`clipType` is its registered element ID and whose arguments are in `params`.
The clip-level `effects` field is for fade timing (`fade_in`/`fade_out`); do
not use it as an unregistered effects namespace.

## Layers and canvas

Visual tracks render in reversed array order: put overlay tracks before the
source track to draw them on top. Use a track per editing concern (for example
brand, captions, effects, b-roll, source) and descriptive clip ids. Read the
[small timeline example](../../../../../examples/hype.timeline.json) for structure.

`output.resolution` and `output.fps` are legacy hints, not the authoritative
canvas. Set `theme_overrides.visual.canvas` when changing the canvas, and match
an explicit render profile to that canvas. The default is 1920x1080 at 30 fps.

### Constrained transparent PNG overlay

The FFmpeg backend accepts one static transparent PNG as a managed media layer
when it is held for the full ordinary picture duration. Put the overlay track
first so it is composited on top, and put exactly one ordinary visual picture
track beneath it:

```json
{
  "tracks": [
    {"id": "overlay", "kind": "visual"},
    {"id": "picture", "kind": "visual"}
  ],
  "clips": [
    {"id": "overlay", "at": 0, "track": "overlay", "clipType": "media", "asset": "managed-alpha.png", "hold": 1.0},
    {"id": "picture", "at": 0, "track": "picture", "clipType": "media", "asset": "managed-video.mp4", "from": 0, "to": 1.0, "volume": 0}
  ]
}
```

The registry entry for `managed-alpha.png` must point to a local PNG whose
decoded dimensions exactly match the visual canvas and whose PNG bytes declare
transparency. Strict support also requires a positive `hold`, timeline start at
zero, no `from`/`to` on the held clip, and the hold to equal the ordinary
picture duration. The layer is looped, bounded to its hold interval, normalized
to the canvas, and composited after the base visual concat; existing text
overlays are then composited using their current path, and stream copy is
disabled for this path.

This subset supports one full-duration static layer. It does not implement
arbitrary transforms, crop, per-layer blending, or multiple held overlays.

## Registry and authority

The `registry` supplied to `create`/`save` is the complete asset registry for
that timeline version. Media references must be runtime-owned object IDs with
verified content digests. The host materializes verified bytes for a render;
durable timeline state must not contain source paths, URLs, or renderer-local
CAS locators. Keep authored storyboard inputs separate from runtime materializations when
using a storyboard compiler.

## RenderProfile v1

`timelines render --profile` accepts one flat JSON object; do not nest `video`
and `audio` mappings. A complete Remotion MP4 profile is:

```json
{"width":1920,"height":1080,"fps_rational":[30,1],"time_base":[1,90000],"container":"mp4","video_codec":"h264","video_profile":null,"video_level":null,"pixel_format":"yuv420p","audio_codec":"aac","audio_sample_rate":48000,"audio_channel_layout":"stereo","duration_tolerance":1}
```

Required fields are `width`, `height`, `fps_rational`, `time_base`, `container`,
`video_codec`, `video_profile`, `video_level`, `pixel_format`, and
`duration_tolerance`. The audio fields are an all-or-none trio. An omitted
profile resolves from the authoritative theme canvas (currently 1920x1080 at
30 fps); an explicit profile must match that canvas. Use current
`timelines render` help as the final authority when profiles evolve.

The default H.264/AAC output needs an `.mp4` filename. Alpha-stamped timelines
may use `.mov` for their declared ProRes 4444/PCM contract. Do not infer a
different extension or codec from a backend alias.

## Visual evidence

Only when the user asks for a render or frames. By default, publish and point
the user to the editor link instead.

`timelines visualize` accepts `png` or `md` formats and always produces the
paired rendered filmstrip. A successful managed render is required;
the returned manifest and pack root are navigation artifacts for that render,
not editable timeline input.

For a successful managed render, `timelines visualize` defaults to
`--view filmstrip`, which opens the primary unified
inspector: composited frame cards plus expandable visual/audio lanes on one
absolute-time ruler. When a render carries audio, bounded waveform bins and
measured low-amplitude “Quiet gap” intervals are available; immutable speech
annotations remain distinct from acoustic gaps and expose canonical/recognized
wording, provenance, uncertainty, and coverage. `--include-media` explicitly
bundles a relative hash-verified video for offline seek/audition. No provider is
called when the inspector opens, and missing audio, stems, or transcript spans
are explicit states. Frame, clip, phrase, and gap selections share render-scoped
targets; the paired filmstrip is the sole timeline visualization surface and
its range/shot/clip/track targets are the canonical navigation controls.
