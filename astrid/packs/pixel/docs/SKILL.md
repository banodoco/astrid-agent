---
name: pixel
description: >
  Turn generated images into true pixel-art sprites: pixel.sprites cuts a
  generated sheet into pieces at the density it was drawn at (with an outline
  check), pixel.snap / pixel.cutout / pixel.strip for single images, cutouts and
  animation strips. Decide a sprite's density in the generation prompt; never
  resample to hit an on-screen size.
---

# Pixel — make sprites from a generated sheet

Choose each sprite's density when generating it. At the film's one integer
art-pixel scale, native width determines on-screen width. Prompt for the
target cell count, for example: **"each icon about 44 pixels wide, every pixel
a perfect square of identical size"**. Then use `pixel.sprites` on the sheet
and place its native RGBA outputs on the timeline at that one scale. Never
resample a finished sprite to hit an on-screen size; that breaks 1-px outlines.

```python
import astrid.sdk as sdk

result = sdk.invoke(
    "pixel.sprites", kind="action", project="almost-ready", wait=True,
    inputs={"image": "run:<run_id>/generated_images#0", "mode": "chroma",
            "cells": "4x2", "scale": 5},
)
result.raise_for_error()
print(result.outputs["artifacts"])  # sprites#0..n, preview, report
```

`image` accepts a generation output row, `run:<run_id>/<port>#n`, or an imported
`sha256:<digest>` media handle. `pixel.sprites` detects the drawn block or accepts
`block` in source pixels, snaps at that density, reuses cutout's `chroma`,
`flat`, or `luma` background removal, and splits connected pieces. Use
`cells: "4x2"` for a regular sheet; omit it for connected components.
`merge_distance` (default 3 native cells) keeps nearby detached details with
their piece. `padding` (default 2) is transparent native cells. The indexed
contact sheet is `preview`; `report` gives source boxes, native and on-screen
sizes at `scale`, and each sprite's dark-outline boundary fraction.

## Other entrypoints

`python3 -m astrid pixel` lists the declared actions:
`pixel.cutout`, `pixel.snap`, `pixel.sprites`, and `pixel.strip`. Invoke an
action with `astrid.sdk.invoke(capability_id, kind="action", project=...,
inputs={...})`; the example above shows the full form for `pixel.sprites`.

| Action | Use |
| --- | --- |
| `pixel.snap` | Snap one image to its drawn lattice (`auto_grid: true`) or an explicit grid; optional palette and nearest-neighbour preview. A grid over 20% coarser than the detected art is refused unless `allow_lossy: true`. |
| `pixel.cutout` | Remove chroma, flat, or light background into hard alpha. `grid: "auto"` snaps first; `trim` defaults true. `holes: true` also removes enclosed key colour. |
| `pixel.strip` | Align two or more frame handles on a common cell and join them horizontally for `am-sprite`. |

When auto detection cannot establish a square lattice, the error reports the
best block hint and an explicit grid recovery. If x and y runs differ, the
report says so and uses their joint square estimate; inspect the preview.
An intentional coarse conversion needs `allow_lossy: true`, and its report
records the override. Prefer regenerating at the target density.
