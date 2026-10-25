# Extend timeline visualize: layers, checks, project rules, data tracks

`timelines visualize` and `timelines lint` are built from four small, registered parts.
You can add each one without touching the rendering pack.

| You want to… | Add | Where | Python? |
|---|---|---|---|
| see a timeline a new way | a **layer** (one PNG panel + findings) | `astrid/packs/<pack>/rendering/visualize_layers/<name>.py` → `LAYER` | yes |
| check a new condition | a **check** (typed findings, optional fix) | same module → `CHECK` (or `Layer(..., checks=(...))`) | yes |
| change thresholds/severities | a **rules file** | `astrid-lint.toml` next to your work | no |
| attach new data to clips | a **data track** | `clip.app.data.<name>` in the timeline document | no (JSON) |
| chart an element's motion exactly | **`motion.ts`** in the element folder → `motionAt` | `astrid/packs/<pack>/elements/<kind>/<id>/motion.ts` | no (TS) |

Discovery: every `astrid/packs/*/rendering/visualize_layers/*.py (or the older */visualize_layers/*.py)` (not starting with `_`) is imported, and its
`LAYER`/`LAYERS`/`CHECK`/`CHECKS` are registered. A module that raises is listed as broken and
the rest keep working. Verify with:

```bash
python3 -m astrid timelines visualize --list-layers      # layers (built-in and pack)
python3 -m astrid timelines lint --list-checks           # checks, their threshold keys and defaults
```

`timelines lint` and `--list-layers`/`--list-checks` run client-side, so a new module works there at once.
`visualize` sheets are drawn by the pack host, which serves the promoted checkout: a new layer shows up in
sheets after its commit is promoted (ask whoever runs `astrid dev promote`).

## 1. A layer

```python
# astrid/packs/<pack>/rendering/visualize_layers/cut_length.py
from PIL import ImageDraw
from astrid.packs.rendering.actions.timeline_visualize.layers import Layer, LayerResult
from astrid.packs.rendering.actions.timeline_visualize.layers.base import PALETTE, draw_text

def render(ctx):
    image = ctx.panel(60, title="cut length")          # a panel on the shared time axis
    draw = ImageDraw.Draw(image)
    x0, x1 = ctx.x_of(float(ctx.cut["start"])), ctx.x_of(float(ctx.cut["end"]))
    draw.rectangle((x0, 30, x1, 50), fill=PALETTE["key"])
    seconds = float(ctx.cut["duration"])
    return LayerResult(image, [f"LENGTH cut {ctx.cut['index']} is {seconds:.2f} s"])

LAYER = Layer("cutlength", "the cut's span on the time axis", render, needs=("doc",))
```

`Layer(name, help, render, needs=("doc",), views=("motion",), experimental=False, default=True,
order=100, checks=())`:
- `name` is one short word (letters, digits, `-`, `_`). `--layer cutlength` selects it.
- `needs`: `"doc"` (timeline data), `"frames"` (captured frames; the planner captures them),
  `"words"`, `"beats"`, `"audio"`.
- `views`: `"motion"` (window/cut sheets) and/or `"contact"`.
- `default=False` makes it opt-in; `experimental=True` runs it only when named.
- `render(ctx) -> LayerResult(image | None, findings: list[str], title="", page="data"|"frames")`.

`ctx` (a `LayerContext`) holds everything in seconds on the timeline clock:

| field | what |
|---|---|
| `cut` | the cut or window: `index, start, end, duration, shot, clip_id, layers, deliberate_hold, sequence`; `window=True` for a range |
| `cuts` | every picture cut |
| `elements`, `all_elements` | clips in the window / in the timeline: `id`, `short_id` (the id in the document, e.g. `c24-02-am-sprite`), `type` (clipType), `track`, `start`, `end`, `params`, `clip` (the raw clip), `asset`, `label`, `audio`, `sequence` |
| `words`, `beats`, `sfx`, `events` | VO words (`start, end, text`); `{"beats","downbeats","hits"}`; `(start, end, name)`; entrances/exits/keys |
| `tracks` | data tracks in the window (see §4) |
| `frames` | `{timeline frame: PNG path}` when the layer needs frames; `ctx.frame_image(n)` opens one |
| `fps`, `window`, `width` | frame rate, the panel time window `(start, end)`, the panel width |
| helpers | `ctx.panel(height, title=...)` (a blank panel with the time grid), `ctx.x_of(t)` (panel x), `ctx.visible(t0, t1)`, `ctx.rel(t)`; colours `PALETTE` keys: `bg, panel, ink, muted, grid, grid_strong, cut, word, word_ink, beat, downbeat, hit, sfx, enter, exit, key, warn, bad, good`; `draw_text(draw, (x, y), text, size, colour)` |
| `shared` | scratch dict shared by the layers of one sheet |

## 2. A check (a condition)

```python
# same module as the layer, or its own: astrid/packs/<pack>/rendering/visualize_layers/long_text.py
from astrid.packs.rendering.actions.timeline_visualize.layers import Check

def run(ctx):
    limit = ctx.param("max_words_on_screen")             # a threshold this check declares (below)
    found = []
    for element in ctx.elements:
        text = str(element.params.get("text") or "")
        if element.type == "am-type" and len(text.split()) > limit:
            found.append(ctx.finding("WORDY", f"{element.label} shows {len(text.split())} words (> {limit:g})",
                                     t=element.start, fix={"clip": element.short_id, "set": {"params.text": "…"}}))
    return found

CHECK = Check("wordy-type", "on-screen type longer than max_words_on_screen words", run,
              params={"max_words_on_screen": 8}, scope="cut", codes=("WORDY",))
```

`Check(name, help, run, params={key: default}, scope="cut"|"timeline", needs=("doc",), codes=())`:
- `run(ctx)` returns a list of `Finding`s (empty = passed). Build them with
  `ctx.finding(code, message, t=..., severity="warn", fix=...)`; severity defaults to `warn`.
  `ctx` is a `CheckContext`: `cut` (`None` for timeline scope), `cuts`, `elements` (in the cut),
  `all_elements`, `words`, `beats`, `sfx`, `tracks`, `fps`, `frames`; read thresholds with
  `ctx.param("key")` (the merged value: your default, overridden by the rules file).
- `params` declares your threshold keys and defaults. `None` means off until a rules file sets it.
  Keys are shared, so another check reading `min_text_px` gets the same value.
- `severity` is `error | warn | info`. `fix` is advisory data for an agent or tool (nothing applies it
  automatically, and keys are not validated): `{"clip": <short_id>, "set": {"params.x": 1104}}` sets
  document paths relative to the clip; `{"clip": <short_id>, "move_s": 0.12}` moves the clip. Use real
  param names from the element's `element.yaml`.
- `needs=("frames",)` runs only inside `visualize --view motion`, where frames exist. Doc checks also
  run in `timelines lint`. A check can ship with a layer, `Layer(..., checks=(CHECK,))`, OR be exported
  as `CHECK`; do one or the other (the same name registered twice keeps the last).

## 3. Project rules: `astrid-lint.toml` (no Python)

Put it in the folder you run `timelines lint` from (or any parent), or pass `--rules FILE`. The first
line of `timelines lint` says which file is in force and its values (`rules: built-in defaults` means none
was found). Finding codes for `[severity]` come from `timelines lint --list-checks`:

```toml
# thresholds: any key from `timelines lint --list-checks`
max_cut_s = 4                 # off by default; cuts marked deliberate_hold are exempt
stamp_to_word_max_s = 0.1
min_text_px = 32
max_presenter_share = 0.3

[severity]                    # per finding code: off | info | warn | error
EDGE = "off"
LONG = "error"

[checks]
disable = ["vo-level"]        # skip whole checks by name
```

An unknown key is an error that lists the known keys. `--list-checks` shows the values in force.
A check with no finding line passed (or is off until a threshold is set); `checks run:` lists them.
`--strict` exits 1 on any `error` finding. `visualize --cut N` uses the same file.

## 4. Data tracks

A data track is typed time data on a clip: `clip["app"]["data"][<name>]`.

```json
{
  "kind": "points",
  "units": "s",
  "time": "clip",
  "source": {"handle": "sha256:…", "producer": "my-tool@1", "params": {}},
  "items": [[0.4, "claw closes"], [1.2, "claw opens"]]
}
```

| kind | items | example |
|---|---|---|
| `points` | `[[t, "label"?], …]` | beats, cues, claw contact |
| `intervals` | `[[t0, t1, "label"?], …]` | words, holds, gestures |
| `series` | `{"t0": 0, "step": 0.05, "values": [...]}` or `[[t, v], …]` | loudness, motion energy |
| `boxes` | `[[t \| null, x, y, w, h, "label"?], …]` (canvas px; `null` = static) | a tracked face |

- `time: "clip"` means seconds from the clip's start (like `app.words`). `"source"` means media seconds,
  mapped through the clip's `from` (like `app.beats`).
- `source` is provenance. `producer` names what made the track (`"my-tool@1"`, or `"hand"` for one
  you typed). `handle` is the media it was computed from, in the media-handle grammar (`sha256:<hex>`,
  `run:<run_id>/<port>#n` or `ref:<name>`); leave it out for a hand-made track.
- Reading: every layer and check gets them in timeline seconds through `ctx.tracks`
  (`motion.data.tracks(...)`). The sync layer draws any points/intervals/series track as a lane.
  The `composition` check uses a `face` boxes track, when present, as the presenter's face.
- Derived tracks (read-only views of older data): `words`, `beats`, `music_hits`, `face_zone`, `presenter_words`.

Find clip ids with `timelines show <timeline> --project P --as code` (each element ends `# id …`).
Attach a track to a checked-out document (a local copy; nothing changes until you publish), then check
and publish as usual. Use a scratch folder of your own for the files:

```bash
python3 -m astrid.packs.rendering.docs.scripts.timeline_document checkout --project P --timeline T --file <scratch>/edit.json
python3 -m astrid.packs.rendering.docs.scripts.timeline_data add --file <scratch>/edit.json --clip v17-04-am-sprite --name claw_contact --track track.json
python3 -m astrid.packs.rendering.docs.scripts.timeline_data loudness --file <scratch>/edit.json   # producer: RMS dBFS per VO/music clip
python3 -m astrid.packs.rendering.docs.scripts.timeline_data list --file <scratch>/edit.json
python3 -m astrid.packs.rendering.docs.scripts.timeline_data validate --file <scratch>/edit.json
python3 -m astrid.packs.rendering.docs.scripts.timeline_document check --file <scratch>/edit.json
python3 -m astrid.packs.rendering.docs.scripts.timeline_document publish --file <scratch>/edit.json --idempotency-key <key>
```

A check reads a track by name:

```python
from astrid.packs.rendering.actions.timeline_visualize.motion import data
for track in data.by_name(ctx.tracks, "claw_contact"):
    for t, label in track.points: ...
```

## 5. Element motion: `motion.ts`
Curves, bounds, lip sync and lint read an element's motion from the element itself. Put the maths the
component draws with in `motion.ts` beside `component.tsx`, have the component call it, and export
`motionAt(params, clipFrame, fps) -> {zoom, pan_x, pan_y, x, y, mouth, …: number}` (pure: no React, no DOM).
visualize evaluates it with the renderer's Node and esbuild for every frame of every such clip (~0.1 s), so
a new param (a push, a pan) shows up with no visualize change. Examples: `am-presenter/motion.ts`,
`am-snap-plate/motion.ts`. Without Node the curves say "element maths unavailable" and use the fallback
model in `timeline_visualize/motion/model.py`.
