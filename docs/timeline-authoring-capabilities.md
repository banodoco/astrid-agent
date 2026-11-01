# Timeline authoring bundle capabilities

The authoring bundle is a detached, exact-head working copy of the existing
Runtime parent/shot/internal-timeline payloads. It is not a second timeline
format. Open a pinned closure, use ordinary Python or the thin SDK helpers,
then validate, preview, and submit the complete candidate through the existing
CAS publication boundary.

```python
from astrid.sdk import (
    open_authoring_bundle, validate_authoring_candidate,
    preview_authoring_candidate, publish_authoring_candidate,
    add_authoring_shot, move_occurrence_group, add_track, place_media, sequence, grid,
)
from examples.timeline_authoring_recipes import brightness_sequence

# `client.timelines.open_composition(project, ref)` is a read-only projection
# for inspection. It does not return the editable bundle.
inspection = client.timelines.open_composition(project_id, timeline_id)
# Open the same exact pinned closure as an editable detached candidate.
work = open_authoring_bundle(parent, shot_revisions=shots,
                             internal_timeline_revisions=timelines)
brightness_sequence(work, admitted_image_ids, brightness_key)
# Move one complete occurrence group while keeping durations and total time.
move_occurrence_group(work, "closing-occurrence", before_occurrence_id="middle-occurrence")
validate_authoring_candidate(work)       # no persistence
preview = preview_authoring_candidate(work)  # frozen candidate artifact
receipt = publish_authoring_candidate(work, writer, idempotency_key="run-1")
```

## Supported authored fields

| Bundle area | Admitted edits | Derived/guarded fields |
| --- | --- | --- |
| `parent` | config, direct clips, registry, layout, extension/opaque fields | `occurrences` is derived from `placements` |
| `placements` | order, local timing, track, explicit x/y/width/height/crop/opacity geometry, gain, mute, provenance, extension fields | `occurrence_id` remains the stable placement identity; `shot_revision_id` is compiler-owned; source offsets/playback rate belong in internal clip edits |
| `shots[*].payload` | item add/remove/reorder, media selection, metadata, pools, bindings, audio, generation inputs, opaque fields | item IDs must be unique; media IDs must be canonical digests |
| `shots[*].internal_timeline` | track/clip structure, timing, geometry, effects, audio, layout, registry alternatives, opaque fields | clip IDs must be unique; selected asset selectors must resolve |

Untouched fields are copied losslessly. Removed alternatives and source rows are
not deleted from immutable Runtime storage. A missing selected asset, duplicate
identity, malformed digest, or dangling registry selector fails validation
before publication. Media import/transcoding and renderer capability
negotiation remain explicit caller responsibilities. The SDK exposes exact
source/local-timeline conversion helpers (`source_to_timeline_time` and
`timeline_to_source_time`) using half-open arithmetic and positive playback
rates. `quantize_interval` returns requested and applied frame/second
boundaries and rejects collapsed ranges. For an intentional timing shift,
`retime_with_ripple(..., scope="track")` moves only later visual clips on that
track; it rejects audio/voice/music tracks and makes parent-duration overflow
explicit (`preserve`, `extend`, or `trim`).

`authoring_contract()["capabilities"]` is the machine-readable version of the
field table above. It distinguishes editable fields, derived fields, guarded
placement fields, and invariants so an agent can inspect capability coverage
without rediscovering the compiler rules.

| Helper | Use |
| --- | --- |
| `place_media` | Add an admitted media item using canonical `at`, source `from`/`to`, optional `hold`, and `speed`. |
| `move` / `retime` | Change a clip's timeline interval while preserving its source origin and playback speed. |
| `sequence` / `align` | Arrange supplied clips on explicit half-open timeline intervals. |
| `retime_with_ripple` | Shift following visual clips on the same track with an explicit duration policy; audio is never moved. |
| `move_occurrence_group` | Move one whole occurrence immediately before another in a contiguous sequence; child shot/clip bindings and durations stay attached to the occurrence. |
| `fit_duration` | Extend or trim the timeline duration to cover its clips. |
| `quantize_interval` | Convert requested boundaries to exact frames and report any rounding. |
| `duplicate` / `replace_media` / `remove` | Manage detached clip, track, shot, or media identities. |

## Four small recipes

These recipes all mutate the same detached objects and use one final candidate
validation/commit. They intentionally avoid a new command language.

```python
from examples.timeline_authoring_recipes import (
    audio_reactive_arrangement, brightness_sequence, source_offset_beats,
    timed_quadrants,
)

# 1. Brightness-sorted images at 100 ms each.
brightness_sequence(work, image_ids, brightness_key)

# 2. Four timed quadrants: stacking is explicit, chronology is unchanged.
timed_quadrants(work, shot_id, quadrant_ids, beats, duration=1.0)

# 3. Supplied beats with source offsets.
source_offset_beats(work, shot_id, beat_rows)

# 4. External audio-reactive arrangement: import first through the existing
# media service, then place the admitted stable ID and edit ordinary fields.
audio_reactive_arrangement(work, shot_id, imported_audio_id,
                           duration=audio_duration, analysis_seed=analysis_seed)
```

`diff_authoring_candidate`, `authoring_media_inventory`,
`inspect_authoring_candidate`, and `preview_authoring_candidate` are read-only.
The bounded inspection reports the exact pinned parent head, candidate and
publication digests, placement pages, selected/alternative media counts, and
explicit unresolved/omitted records without copying opaque payloads. The
inventory reports selected media, registry metadata/alternatives, usage paths,
unresolved selectors, and a stable `source_ref` (`project_id`, digest, and
Runtime object ID). A caller opens the source through the existing service
(`client.media.show(project_id, object_id)` for metadata or
`client.media.read_bytes(object_id)` for bytes); the inventory itself never
selects or mutates media. Its `composed_ref` links the same rows to the
candidate digest and pinned parent head. The composed preview is a separate
renderer concern and must retain that candidate/head identity.
