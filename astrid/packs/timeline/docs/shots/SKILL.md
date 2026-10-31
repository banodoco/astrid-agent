---
name: timeline-shots
description: >-
  Label timeline sections, store them as shots, and reuse a stored shot in
  another occurrence.
---
# Store and reuse shots

Start with a timeline that has a picture track, one authored picture clip, and
two picture cuts at the section boundaries. The example labels the sections,
stores them as shots, places the opening shot again, and arranges the three
occurrences.

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
