---
name: timeline-show
description: Read a timeline as structure and text - cuts, clips, words, assets, plans and history.
---
# Show a timeline

Astrid commands here run through the launcher; its path on each OS is in the [astrid-setup skill's Launcher paths](../../../setup/docs/SKILL.md#launcher-paths), and the commands show the Linux path as `run-astrid`.

## Philosophy
Before you look at pixels or change anything, read what the timeline *is*. Show is cheap, exact and text-only: it answers "what is where" with ids and times you can act on. It never renders and never changes anything. Use it to find the thing you mean, then visualize it, then edit it.

## Practical
| Question | Command |
|---|---|
| Which timelines exist? | `run-astrid timelines list --project P --recent` |
| What happens when, cut by cut? | `run-astrid timelines show T --project P --as cuts` |
| The script: lines and who says them | `run-astrid timelines show T --project P --as script` |
| One clip in detail (source range, speed, media) | `run-astrid timelines show T --project P --clip <clip> --detail` |
| Which media files the cut uses | `run-astrid timelines assets T --project P` |
| The words, with times | `run-astrid timelines words T --project P` and `timelines lines` |
| What each shot is meant to achieve | `run-astrid timelines shots plan list --project P --timeline T` |
| What changed, and when | `run-astrid timelines history T --project P` |

## Example (Into the Minkhole)
Track names describe today's media, not the target: the approved cut's voice track is "Temporary voiceover — Daniel" (a system voice), while each shot's plan says what the final voice must be (Morpheus).

`timelines shots plan list` on the approved cut lists nine shots with their methods: "02c Blue pill - Morpheus speaks: LC+R, planned, 'tuning workflows by hand,'". `timelines assets` shows that one voice file, `blue`, plays in four clips across four shots. `timelines show --clip blue-voiceover-part-3 --detail` gives its source range (3.967-8.258 s of that file).

## Next
See it with `timeline-visualize`; change it with `timeline-editing`; words in depth with `timeline-transcripts`; plans with `timeline-shots`.
