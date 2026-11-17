---
name: timeline-visualize-advanced
description: Study one shot closely - every frame, words against picture, a region over time, mask fit and a take against its source.
---
# Visualize a shot in depth

Astrid commands here run through the launcher; its path on each OS is in the [astrid-setup skill's Launcher paths](../../../setup/docs/SKILL.md#launcher-paths), and the commands show the Linux path as `run-astrid`.

## Philosophy
Editing checks ask "does the cut look right?". Generation and masking ask harder questions about one shot: where does it move, when exactly is each word said, will a fixed mask hold, does a take match its source at the seams. Each view below is named by the question it answers.

## Views, by question
| Question | Command |
|---|---|
| Every frame of a short span (smoothness, a stutter, a mouth) | `run-astrid timelines visualize T --project P --preset motion --range 12.2..13.2 --every-frames 1` |
| When is each word said, against the picture? | `run-astrid timelines visualize T --project P --cut 8 --layer sync` (needs stored words; see `timeline-transcripts`) |
| Does the mouth move with the words? | `--cut 8 --layer lipsync` |
| A detail at source resolution (lenses, mouth) | `--at 14 --region X,Y,W,H` (or `--zoom <layer>`) |
| Does a region move across the shot? | `--cut 8 --region X,Y,W,H` (or `--range A..B`, `--shot S`) |
| Will this mask hold? | `--cut 8 --mask <media>` (with `--shot S`, the plan's `inputs.masks`) |
| Where does the picture move? | `--cut 8 --layer heat` |
| Does a take match its source? | `--cut 8 --compare-media <take> [--against <source media>] [--mask <media>] [--region …]` |
| What layers exist | `--list-layers`: sync, curves, still, diff, lipsync, lint, strip, onion, heat, bounds, labels |

Note: `--layer diff` and the motion view's "pixel change" chart show *how much* changed per frame (red = frozen). They catch freezes and stutters in edits; they do not show *where* the picture moves.

## Understanding a clip
All boxes are canvas px (the timeline's own canvas, e.g. 1280x720; `timelines show` prints it). These views capture at the canvas size, so a crop is the source resolution, and they draw from the composed frames of the saved revision.

- **Region over time.** `--region X,Y,W,H` with `--cut`, `--range` or `--shot` draws the motion map of the region first, then the region at four moments across the span (smaller when four do not fit at 1:1; `--at T --region …` gives one at full size), each labelled with how far its content has moved from the first moment, and says `holds` (moves at most 4 px), `tight` (up to 12 px: give a fixed mask that much margin or feather) or `moves` (track it). Names work too: `mask` (the mask's area), `top`, `bottom`, `left`, `right`, `centre`, `top-left` …
- **Mask fit.** `--mask <media id>` (an image, white = edited; a short id such as `sha256:5da3b6fbe0eb` is enough) draws the mask's edge in cyan at the same place on every moment, so anything that slides off the line is drift, and states how much of the span's motion falls inside vs outside the mask and whether pixels just outside its edge keep changing (the masked thing crossing it). Without `--region` the mask's own area is shown.
- **Motion map.** `--layer heat` greys the cut's middle frame and colours each spot by how often it changes (violet now and then, yellow every frame); a box marks the strongest change. Every clip view also writes this map as a second page, with the mask edge on it. It answers *where*; `--layer diff` answers *how much, when*.
- **Take vs source.** `--compare-media <take>` shows the source, the take and where they differ, at four moments, with numbers away from the mask, along its edge (12 px either side: the seam) and inside. The source is the composed cut by default; `--against <media id>` reads that media instead (the cut's own picture media is read at the clip's source time and speed). The take's second 0 is the span's start. With `--shot S`, `--compare-media take` uses the plan's latest take. Any media item works, on the timeline or not.

Each run prints the page path last; the other pages (the motion map, the plain motion sheet) are listed as `also:`. The `next:` lines carry your flags forward.

## Example (Into the Minkhole)
Shot 02c, the Morpheus close-up (cut 8, 12.17-16.46 s, canvas 1280x720): `--cut 8 --region 300,0,980,450 --mask <lens mask>` says *tight: it moves up to 8 px (most at 13.67 s)*, so a fixed lens mask needs about 8 px of feather; the motion map puts the strongest change on the mouth (lower centre), not the lenses. After the masked edit, `--cut 8 --region 300,0,980,450 --mask <lens mask> --compare-media <take>` shows whether the take leaves the face alone and whether the edit spills over the mask's edge; then check the mouth against the words with `lipsync`.

## Next
Back to `timeline-visualize` for whole-film looks; `h3-av-recast-scene` for the shot methods these views judge.
