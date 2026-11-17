---
name: timeline-editing
description: Change a timeline safely - commits, one undo each, retry on conflict, then look at the result.
---
# Edit a timeline

Astrid commands here run through the launcher; its path on each OS is in the [astrid-setup skill's Launcher paths](../../../setup/docs/SKILL.md#launcher-paths), and the commands show the Linux path as `run-astrid`.

## Philosophy
Every change is a commit to the timeline: one commit is one undo, it lands on the latest revision, and if someone else changed the timeline first it is retried on top of their change rather than overwriting it. Edit only what you have found with `timeline-show` and seen with `timeline-visualize`, then look again at the moments you changed. Never edit timeline files by hand; go through Astrid.

## Practical
| Task | Where |
|---|---|
| Open, change and commit (trim, move, add, remove clips; titles) | The quick start in the parent `timeline` skill (`Timeline.open(...)`, `tl.commit(fn, label=..., idempotency_key=...)`) |
| A detached working copy for several edits, then commit | `run-astrid timelines checkout` / `commit` / `discard`; `timelines check --at` compares it with the published timeline |
| Note why a cut is the way it is | `run-astrid timelines edit T --project P --cut c12 --why "..." --hold` |
| Placement and fit of clips (fill the frame, no black bars) | `run-astrid timelines appearance` |
| A shot's plan | `run-astrid timelines shots plan set <shot> --project P --timeline T --set method=LC,R` (see `timeline-shots`) |
| Recipes for common edits | the timeline cookbook and editing references in this pack |

## Example (Into the Minkhole)
Writing the nine shot plans onto the approved cut was nine commits through `timelines shots plan set`; each is a revision you can read back with `timelines history` and `plan show --revision-id`. After editing, `timelines visualize --preset overview --shot-plans` showed all nine shots planned.

## Next
Words-led editing: `timeline-narrated`; values approved by a person first: `timeline-review`; one edit across many timelines: `timeline-series`.
