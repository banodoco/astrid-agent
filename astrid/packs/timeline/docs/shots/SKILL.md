---
name: timeline-shots
description: >-
  Label timeline sections, store them as shots, and reuse a stored shot in
  another occurrence.
---
# Store and reuse shots

Start with a timeline that has an authored picture clip. Split it at each
section boundary, label those cuts, and restructure the timeline to store each
section as a shot.

```python
from pathlib import Path

from astrid.sdk.timeline.api import Checkout, Timeline
from astrid.sdk.timeline_structure import label_chapters
from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_add_media
from astrid.sdk.timeline.native_occurrence_shared import capture_occurrence_place_shared

tl = Timeline.open(project, timeline)
client = tl.session.client
tl.commit(lambda t: t.track("picture", kind="visual"), label="Prepare picture track",
          key="shots-picture-track")
birth = capture_occurrence_create(
    tl.bundle, "opening-source", "opening-source-shot", name="Opening source",
    track="picture", on=0, duration=4,
)
tl.commit([birth], label="Create source shot", key="shots-source-shot")
track = capture_track_create(tl.bundle, "occurrence:opening-source", {"id": "plate", "kind": "visual"})
tl.commit([track], label="Create shot picture track", key="shots-shot-picture-track")
transport = client._remote._transport
imported = transport.import_project_media(
    project, Path(picture).read_bytes(), media_type="image/png", filename=Path(picture).name,
    idempotency_key="shots-picture-import",
)["data"]
media = transport.get_project_media_selection(project, {
    "asset": imported["variant_id"], "generation_id": imported["generation_id"],
    "variant_id": imported["variant_id"],
})
verified = transport.get_project_verified_media(project, media)
seed = capture_occurrence_add_media(tl.bundle, "occurrence:opening-source", {
    "id": "opening-seed", "clipType": "image", "track": "plate",
    "asset": media["asset"], "at": 0, "hold": 4,
}, media, verified_media=[verified])
tl.commit([seed], label="Add source picture", key="shots-source-picture")
checkout = Checkout.open(project, timeline, client=client)
checkout.clip("opening-seed").remove()
picture_clip = checkout.add("media", at=0, hold=4, asset=media["asset"], track="plate",
                            cut="c1", layer="picture")
second_cut = checkout.add_cut(2, after="c1", id="c2")
picture_clip.hold_for(2)
second_cut.picture.on(None)
checkout.publish("Author picture cuts", idempotency_key="shots-author-picture-cuts", client=client)
tl = Timeline.open(project, timeline)

cuts = sorted(tl.cuts, key=lambda cut: cut.start)
labels = [
    {"name": "01 Opening", "at": cuts[0].start},
    {"name": "02 Ending", "at": cuts[1].start},
]
tl.commit(lambda t: label_chapters(t, labels), label="Label sections", key="shots-label-sections")
checkout = Checkout.open(project, timeline, client=client)
checkout.publish("Store sections as shots", restructure=True,
                 idempotency_key="shots-restructure", client=client)

tl = Timeline.open(project, timeline)
section = next(shot for shot in tl.bundle["shots"].values()
               if shot["payload"]["name"] == "01 Opening")
source = next(row for row in tl.bundle["placements"] if row["shot_id"] == section["shot_id"])
other = next(row for row in tl.bundle["placements"]
             if row["occurrence_id"] != source["occurrence_id"])
reuse = capture_occurrence_place_shared(
    tl.bundle, f"occurrence:{source['occurrence_id']}", "opening-repeat", on=4,
)
tl.commit([reuse], label="Reuse opening", key="shots-reuse-opening")

ids = [source["occurrence_id"], "opening-repeat", other["occurrence_id"]]
tl.commit(lambda t: t.arrange(ids, at=0, gap=0.5), label="Arrange opening shots",
          key="shots-arrange-opening")
tl = Timeline.open(project, timeline)
placements = sorted(tl.bundle["placements"], key=lambda row: row["placement"]["start_ms"])
canonical = {row["occurrence_id"]: row
             for row in tl._runtime_document["parent_revision"]["payload"]["occurrences"]}
assert canonical[source["occurrence_id"]]["shot_revision_id"] == canonical["opening-repeat"]["shot_revision_id"]
print([(row["occurrence_id"], row["shot_id"], row["placement"]["start_ms"] / 1000)
       for row in placements])
```

The repeated occurrence keeps the same stored shot revision; arrange and inspect placements by occurrence ID.
