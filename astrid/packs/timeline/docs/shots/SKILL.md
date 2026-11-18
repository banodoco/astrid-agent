---
name: timeline-shots
description: >-
  Label timeline sections, store them as shots, and reuse a stored shot in
  another occurrence.
---
# Store and reuse shots

Start with a timeline that has a picture track, one authored picture clip, and
two picture cuts at 0 and 2 seconds. Label the sections, store them as shots,
place the opening shot again, and arrange the three occurrences.

```python
from astrid.sdk.timeline.api import Checkout, Timeline
from astrid.sdk.timeline_structure import label_chapters
from astrid.sdk.timeline.native_occurrence_shared import capture_occurrence_place_shared

tl = Timeline.open(project, timeline)
cuts = sorted(tl.cuts, key=lambda cut: cut.start)
labels = [
    {"name": "01 Opening", "at": cuts[0].start},
    {"name": "02 Ending", "at": cuts[1].start},
]
tl.commit(lambda t: label_chapters(t, labels), label="Label sections")

checkout = Checkout.open(project, timeline, client=tl.session.client)
checkout.publish("Store sections as shots", restructure=True, client=tl.session.client)

tl = Timeline.open(project, timeline)
source = next(row for row in tl.bundle["placements"]
              if row["shot_id"] and row["placement"]["start_ms"] == 0)
other = next(row for row in tl.bundle["placements"]
             if row["occurrence_id"] != source["occurrence_id"])
reuse = capture_occurrence_place_shared(
    tl.bundle, f"occurrence:{source['occurrence_id']}", "opening-repeat", on=4,
)
tl.commit([reuse], label="Reuse opening")

ids = [source["occurrence_id"], "opening-repeat", other["occurrence_id"]]
tl.commit(lambda t: t.arrange(ids, at=0, gap=0.5), label="Arrange opening shots")
tl = Timeline.open(project, timeline)
placements = sorted(tl.bundle["placements"], key=lambda row: row["placement"]["start_ms"])
canonical = {row["occurrence_id"]: row
             for row in tl._runtime_document["parent_revision"]["payload"]["occurrences"]}
original = canonical[source["occurrence_id"]]
repeat = canonical["opening-repeat"]
assert original["shot_id"] == repeat["shot_id"]
assert original["shot_revision_id"] == repeat["shot_revision_id"]
print([(row["occurrence_id"], canonical[row["occurrence_id"]]["shot_id"],
        row["placement"]["start_ms"] / 1000) for row in placements])
```

The repeated occurrence shares the stored shot, so edits to that shot appear in both placements.

## Shot plans

A shot's plan (what it shows, how it will be made, its inputs, status and takes) lives in one place:
the key `plan` in the shot's own metadata on the timeline (`payload.metadata.plan`). It is saved
with every timeline revision (each write is a new revision; earlier revisions keep the earlier
plan) and it travels in `.astrid` exports and imports. Every skill
and agent reads and writes plans **only through the commands below**, never by editing metadata
by hand, so the shape stays checked and other metadata stays untouched.

The shape (`version` 1; every other key is optional; any other key is refused):

```json
{
  "version": 1,
  "on_screen": ["Morpheus"],
  "line": "You have two options.",
  "intent": "new line in the real shot, old man in the lenses",
  "method": ["LM", "R"],
  "check": "mouth follows the voice track; lenses show the old man",
  "inputs": {"references": ["<media id>"], "voice": {"media": "<media id>", "range": [5.0, 8.0]}, "masks": ["<media id>"]},
  "status": "planned",
  "takes": [{"generation": "<run id>", "media": "<media id>", "note": "first take"}]
}
```

- `method`: one or more of the recast method codes `V LC LM R M E K` (see the [recast skill](../../../h3_av/docs/recast-scene/SKILL.md#stage-3---shot-list-pick-a-method-for-each-shot)); `K` (keep) stands alone.
- `status`: `planned`, `in_progress`, `take_ready` or `approved`. `take_ready` and `approved` require one take covering every planned method. A `K` shot can be approved without a take.
  After checking a take in visualize, record the verified method codes in its `covers` list.
- `line` is a label for planning. The narration itself is the shot's `voiceover_script` text
  (`timelines shots text set --kind voiceover_script`); address it by shot and kind, never by binding id.
- `inputs.voice.range` is `[start, end]` in **seconds into the voice media `inputs.voice.media`**
  (the voice track file), not timeline time. A range needs its `media`.

Commands run through the launcher; its path on each OS is in the
[astrid-setup skill's Launcher paths](../../../setup/docs/SKILL.md#launcher-paths), and the block
shows the Linux path. Write, read, list and see:

```bash
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" timelines shots plan set <shot> --project <project> --timeline <timeline> --set method=LM,R --set status=planned
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" timelines shots plan set <shot> --project <project> --timeline <timeline> --json-file plan.json
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" timelines shots plan set <shot> --project <project> --timeline <timeline> --set inputs.voice.media=<media id> --set inputs.voice.range=5,8
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" timelines shots plan set <shot> --project <project> --timeline <timeline> --add-take '{"generation": "<run id>", "media": "<media id>", "covers": ["LM", "R"]}' --set status=take_ready
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" timelines shots plan show <shot> --project <project> --timeline <timeline>
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" timelines shots plan list --project <project> --timeline <timeline>
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" timelines visualize <timeline> --project <project> --preset overview --shot-plans
```

`<shot>` is a shot id, an occurrence id or the exact shot name; an exact shot id always wins over a
name, and a name two shots share is refused (use the id). `set` merges: the fields you give
replace those fields (`inputs` and `inputs.voice` merge key by key), `--add-take` appends,
`--unset inputs.voice` removes a field, `--replace` starts from an empty plan. A bad plan is refused
with the reason before anything is written. If the timeline changes while the plan is being written,
`set` fails with `conflict` and keeps nothing: run the same command again. If it fails with
`outcome_unknown`, the write may or may not have landed: re-run the same command with the
`--idempotency-key` the message names to confirm. `show` and `list` take `--revision-id` to read a saved
revision. `list` also names the shots with no plan (`unplanned`) and the planned shots not yet
`approved` (`not_approved`; `K` shots need no approval). A stored plan that no longer validates is
listed under `invalid` and shown as `invalid plan (...)`; rewrite it whole with `--replace`. `visualize --shot-plans` prints one line per
placed shot under the picture's summary: its time, name, method codes, status, takes and line. From
Python, the same calls are `astrid.sdk.shot_plan.set_plan`, `show_plan` and `list_plans`.
