---
name: timeline-visualize-advanced
description: Study one shot closely - every frame, words against picture, a region over time, mask fit and a take against its source.
---
# Visualize a shot in depth

Astrid commands here run through the launcher; its path on each OS is in the [astrid-setup skill's Launcher paths](../../../setup/docs/SKILL.md#launcher-paths), and the commands show the Linux path as `run-astrid`.

## Philosophy
Editing checks ask "does the cut look right?". Generation and masking ask harder questions about one shot: where does it move, when exactly is each word said, will a fixed mask hold, does a take match its source at the seams. Each view below is named by the question it answers. Draft: items marked *being built* are in progress (branch p59/visualize-readable) and are not yet in a release.

## Views, by question
| Question | Command |
|---|---|
| Every frame of a short span (smoothness, a stutter, a mouth) | `run-astrid timelines visualize T --project P --preset motion --range 12.2..13.2 --every-frames 1` |
| When is each word said, against the picture? | `run-astrid timelines visualize T --project P --cut 8 --layer sync` (needs stored words; see `timeline-transcripts`) |
| Does the mouth move with the words? | `--cut 8 --layer lipsync` |
| A detail at source resolution (lenses, mouth) | `--at 14 --region X,Y,W,H` *(being built)* |
| Does a region move across the shot? | `--cut 8 --region X,Y,W,H` strip *(being built)* |
| Will this mask hold? | `--cut 8 --mask <media>`: mask outline and motion inside vs outside *(being built)* |
| Where does the picture move? | `--cut 8 --layer heat`: a heat map on the frame *(being built; draws nothing today)* |
| Does a take match its source? | a take beside its source at the same moments, cropped to the mask *(being built)* |
| What layers exist | `--list-layers`: sync, curves, still, diff, lipsync, lint, strip, onion, heat, bounds, labels |

Note: `--layer diff` and the motion view's "pixel change" chart show *how much* changed per frame (red = frozen). They catch freezes and stutters in edits; they do not show *where* the picture moves.

## Example (Into the Minkhole)
Shot 02c, the Morpheus close-up (cut 8, 12.17-16.46 s): before making a fixed sunglasses mask, look at the lens region across the cut to see whether his head moves out from under it; then check the mask overlay; after the masked edit, compare the take with the source cropped to the lenses and check the mouth against the words with `lipsync`.

## Next
Back to `timeline-visualize` for whole-film looks; `h3-av-recast-scene` for the shot methods these views judge.
