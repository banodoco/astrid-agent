---
name: timeline-visualize
description: Look at a timeline as pictures and sound without rendering a video - the overview, a moment, a range, a comparison.
---
# Visualize a timeline

Astrid commands here run through the launcher; its path on each OS is in the [astrid-setup skill's Launcher paths](../../../setup/docs/SKILL.md#launcher-paths), and the commands show the Linux path as `run-astrid`.

## Philosophy
A commit proves what is stored; it does not prove what the film looks like. `timelines visualize` draws composed frames of a saved revision in seconds, with no render. Look before you edit, look after every visual edit, and open every page you draw: a page you did not open is not a look. Each command prints the page path last. If visualize cannot show what you need, that is a gap to fix or file, not a reason to use another tool.

## Four looks, by question
| Question | Command |
|---|---|
| The whole film: order, coverage, obvious mistakes | `run-astrid timelines visualize T --project P --preset overview --shot-plans` |
| One moment, at full size (text, placement, detail) | `run-astrid timelines visualize T --project P --at 14 --size 1920x1080` |
| A cut or range in motion (entrances, transitions, timing) | `run-astrid timelines visualize T --project P --cut 8` or `--preset motion --range 11.6..13.4` |
| Before and after an edit | `run-astrid timelines visualize T --project P --preset compare --from <rev> --to <rev> --range 11..14` |

Timing: an overview of a long film can take a couple of minutes and may move to the background; the page path is printed when it finishes. If another task is using the Runtime, a look waits behind it.

Limits worth knowing: the overview holds up to 120 cuts per page (page on with `--cuts`); `--preset motion` shows up to 3 s at every second frame, `--every-frames 1` every frame (up to 60); a plain `--range` scans up to 30 s at two frames a second, too coarse for an effect under a second. An entrance can be invisible on its first frame: look two frames in. `--view diff --from --to` compares the documents only (which cuts changed), not pixels. `--plan` shows what a view would capture without capturing.

Pin what you looked at with `--revision-id` (from `timelines history`) when it matters, and say in your report which revision and moments you looked at.

## What to look for
- Missing media, black frames, a placeholder tile, an effect that does not appear. For a black frame, check with `timeline-show` that a clip covers the moment, then look two and ten frames later before concluding. If media is missing, ask for the file; never substitute one.
- An effect playing the wrong way (an exit as an entrance, a slide from the wrong side).
- Text you cannot read at output size, cut off, or outside the safe margins (`--highlight <layer>` draws them).
- The wrong layer on top, or a crop or opacity you did not mean; changes outside the moments you edited.
If you could not see something, say so. Give the person the editor link and the times to check: your frames do not replace their judgment.

## Reading the overview
One tile per cut: cut number and time, shot name, the words under it ("no words stored" when none), and the shot plan (methods and status). The coloured band is the shots in time; under it, the audio (voice, music, sound effects).

## Example (Into the Minkhole)
The overview of the approved cut shows 19 cuts in 9 shots: Morpheus speaking in cuts 5 and 8, the old man's reaction stills in cuts 7 and 9, and lens close-ups in 13-15. `--at 14 --size 1920x1080` shows Neo reflected in both lenses with the blue pill in the right one, which is why shot 02c's plan includes R (replace the reflection).

## Next
For close study of a shot (frame by frame, words against picture, a region over time, masks, a take against its source) load `timeline-visualize-advanced`. To add a view, see the rendering pack's visualize extension guide.
