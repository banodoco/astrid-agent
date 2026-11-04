# How to look at a timeline edit

A commit result and a read-back prove what is stored: ids, tracks, times.
They do not prove what the film looks like. To know that, look: `timelines
visualize` draws composed frames of a saved revision in seconds, without
rendering a video. Use it freely and often, in whatever way answers your
question. You do not need to be asked. Render a video only when the person
asks for a render or a file.

## When to look

- **Before** a visual edit, when you need to see what is there now.
- **After** every visual edit (placement, text, crop, opacity, layers,
  effects, transitions, timing), at the moments where the change should show.
- **After a correction**, at the same moments again.
- **Before you answer** "does it look right?" or say the work is done.

Each run prints the path of the page it drew. Open that image and look at it;
a page you did not open is not a look. (`timelines check --at` and `--sweep`
compare a detached working copy with the published timeline; after a commit,
use `visualize` as below.)

## Pin what you looked at

Look at a named revision so you know what you saw. In `timelines history
<timeline> --project <project> --json`, the row whose `publication_id` is your
receipt has your revision as `id` and the one before it as `parent`. Pass it
with `--revision-id` when you need to pin the view. Without it, `visualize`
defaults to the published revision. The result prints the revision it drew; if
it says `revision unavailable`, say so rather than guess. In your report, give
the revision, the moments you looked at and what you saw.

## Point at a moment

`--at`, `--range` and `--highlight` take seconds (`12.5`, `11.6..13.4`), a clip
id (`logo`), a layer address (`c12.title`), a cut (`c12`) or a spoken word, on
flat and narrated timelines alike. `--at` a time or cut start gives one frame;
`--at` a clip shows its whole span. `+2f` or `-0.5s` shifts a moment:
`--at 'c12 +2f'`. `--cuts` and `--edited` take comma-separated cut numbers;
the overview and `--view diff` number the picture of any timeline in cuts,
flat ones included, so use the numbers they print.

## Four ways of looking

Choose by the question. Views 1, 2 and 4 take `--revision-id <id>` and
`--plan` (what it would capture, without capturing); view 3 takes the two
revisions.

**1. Overview: order, coverage, obvious mistakes.** One tile per picture cut,
a settled frame from each:

```bash
python3 -m astrid timelines visualize <timeline> --project <project> --preset overview
python3 -m astrid timelines visualize <timeline> --project <project> --preset overview --cuts 121,122,123
```

Tiles are small and a settled frame usually misses transitions, entrances and
exits. One page holds at most 120 cuts; for a longer film, page through the
rest with `--cuts`. If it says it left moments out, look at those with a
range. An overview never proves that text is readable.

**2. A range in motion: transitions, entrances, exits, fades, timing.**

```bash
python3 -m astrid timelines visualize <timeline> --project <project> --preset motion --range 11.6..13.4
python3 -m astrid timelines visualize <timeline> --project <project> --preset motion --range 11.8..12.8 --every-frames 1
python3 -m astrid timelines visualize <timeline> --project <project> --range 8..20
python3 -m astrid timelines visualize <timeline> --project <project> --at logo
python3 -m astrid timelines visualize <timeline> --project <project> --cut 12
```

`motion` shows up to 3 s at every second frame; `--every-frames 1` shows every
frame (up to 60), for smoothness or a stutter. Start the window before the
effect and end it after, so you see the neighbour on each side of a cut and
the effect's whole interior, not just its first or last frame; when you do
not know exactly where an effect sits, start wide. A plain
`--range` scans up to 30 s at two frames a second: good for timing across
several cuts, too coarse for an effect under a second. `--at` with a clip
shows its whole span: where it enters, holds and leaves. `--cut 12` is that
cut's motion sheet, dense after each entrance, exit and change.

**3. Compare two revisions: what changed, and how it looks.** First find
what changed, then see it from both sides at the same moments:

```bash
python3 -m astrid timelines visualize <timeline> --project <project> --view diff --from <before> --to <after> --edited 4,5
python3 -m astrid timelines visualize <timeline> --project <project> --preset compare --from <before> --to <after> --range 11..14
```

`--view diff` compares the documents only: it lists changed cuts and warns
about changes outside `--edited`, but it shows no pixels and is not visual
proof. `--preset compare` with a window draws that window from both
revisions, before on top and after below. To compare two timelines, such as
a master and its variant, run the same view at the same moments on each.

**4. One full-size frame: text and placement.**

```bash
python3 -m astrid timelines visualize <timeline> --project <project> --at 12.5
python3 -m astrid timelines visualize <timeline> --project <project> --at 12.5 --size 1920x1080 --highlight title
```

`--at` a time gives one 1280x720 frame. When text is small or placement is close to
an edge, capture at the output size with `--size`. `--highlight` outlines one
layer and, like `--layer bounds`, draws the title-safe and action-safe
margins. An entrance can be invisible on its first frame; look two frames in.

Mix them. The opening of every scene (`--cuts 1,12,30` for a row of tiles,
then `--cut 12` or `--at 'c12 +2f'` for each opening), a frame-by-frame pass
over half a second, a comparison of a title before and after a fix, or a
full-size frame of each caption are all good uses. Across many timelines,
look at three to five (the first, the last, any whose result was `merged` or
`rebased`, any that looked odd) and say which you did not look at.

## What to look for

- Missing media, black frames, a grey placeholder or "missing media" tile, an
  effect or transition that does not appear. If a frame is black, check with
  `show --revision-id` that a clip covers that moment, then look two and ten
  frames later and across a range before you conclude. If media is missing,
  ask the person for the file; do not substitute one.
- An effect playing the wrong way: an exit that plays as an entrance, a slide
  from the wrong side.
- Text you cannot read at output size, text cut off or outside the title-safe
  margin.
- The wrong layer on top, or a crop or opacity you did not mean.
- Changes outside the moments you edited.

If something looks wrong, fix it and look again. If you could not see
something (an omitted moment, text too small to judge), say so. Give the
person the editor link and the times to check as well; your frames do not
replace their judgment.
