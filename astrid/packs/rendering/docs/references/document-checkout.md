# Check out a timeline, edit JSON, check it in

Use this for an ordinary user-authorized edit starting with a project and
timeline slug, without a coordinator-supplied target. A checkout is a detached
snapshot, not a lock. The runtime remains authoritative until publication.

The complete executable example is
[`../scripts/timeline_document.py`](../scripts/timeline_document.py). It defines
all connection, discovery, immutable-closure, validation, and writer variables;
read it when adapting the workflow. `AstridClient.open_from_launcher` resolves
project/timeline names and pins the head. The public launcher receipt and
`WorkspaceClient` supply the revision reads and publication port that the
high-level client does not yet expose as a general checkout method. It never
constructs a pretend coordinator capability or accesses private client fields.

Run with Python from the environment where Astrid is installed. Keep the same
launcher configuration as Reigh; for a checkout-backed installation this may
mean setting `BANODOCO_LOCAL_DATA_ROOT` to its already-configured data root.
Do not guess another root when the expected project is absent. Use project and
timeline **slugs or IDs**, not display titles.

```bash
# From this skill directory; replace the two names with the selected scope.
python3 scripts/timeline_document.py checkout \
  --project astrid-intro --timeline main-final-blackend2 \
  --file /tmp/timeline-edit.json

# Edit /tmp/timeline-edit.json with a text editor or a Python transformation.
python3 scripts/timeline_document.py check --file /tmp/timeline-edit.json

# After inspecting the resulting .check.json and completing the requested edit:
python3 scripts/timeline_document.py publish --file /tmp/timeline-edit.json \
  --idempotency-key intro-edit-pass-01
```

`checkout` reads the full pinned parent → shot revisions → internal timelines,
not the bounded `timelines show` projection. `check` probes every referenced
local file, prepares a detached copy with calculated media digests, validates,
and diffs locally. It does not connect to or write runtime state. Its `.check.json`
lists the files and import keys that publication will use. `publish` repeats
that preflight, rechecks the selected head, imports through the existing project
catalog, validates the durable result, and sends one parent-CAS publication.
Keep its `.prepared.json` durable rewrite, `.imports.json` catalog receipts,
and `.publication.json` receipt,
including `new_head` and `dependency_manifest`; use a new checkout for the next
edit. Choose a distinct idempotency key for each intended publication.

After publication, report the returned `update`: it counts selected clips by
identity, with additions/replacements/removals separate in `summary`, and keeps
catalog imports/reuse separate from timeline selection changes. Forward the
summary's `Other properties changed` fallback for crop, volume, effects,
metadata, order, and unknown authored properties; it is not an edit count.
Verified mirror-only representation changes are media bookkeeping. The result's
`artifacts` references the actual local check report, prepared document, and
publication receipt. Its check-report action opens indexed property differences
and the import plan; retain these files explicitly, especially for `/tmp`
checkouts. These files are not automatically stored by the Runtime and are not
a guaranteed complete historical diff when whole-shot baselines were removed.
For the exact saved batch, use the returned `saved_revision_pair` actions:

```bash
python3 -m astrid timelines diff <timeline> --project <project> \
  --from-revision <receipt-old-head> --to-revision <receipt-new-head> --format readable
# Every changed field, including revision/hash/reference bookkeeping:
python3 -m astrid timelines diff <timeline> --project <project> \
  --from-revision <receipt-old-head> --to-revision <receipt-new-head> --format details
```

This reads both complete immutable parent/shot/internal compositions, verifies
their pins and digests, and returns stable-identity before/after property details
with a semantic summary. Whole added/removed shots and unknown properties remain
visible even after local checkout files disappear or the current head advances.
Readable output groups by placed occurrence and clip identity, using names from
the pinned state, and lists unknown properties with before/after values. Details
prints every raw change in full; raw field/record counts are not semantic edit
counts. Only proven revision/hash fields are moved to bookkeeping. Ambiguous
registry/item references stay visible at their owning scope. Units or item-to-clip
bindings are not guessed. The default and `--json` retain the exact SDK envelope;
`--format json` is equivalent. Do not combine `--json` with `--format`.
The equivalent SDK call is `client.timelines.diff(project, timeline,
from_revision=old_head, to_revision=new_head)`. Use actual receipt IDs; missing
historical dependencies are errors, never substituted with current state.
For an embedded report, pass its successful `data` to
`astrid.sdk.authoring_bundle.format_authoring_revision_diff(data, mode="readable")`
or `mode="details"`; this is the same pure formatter the CLI uses.
The comparison covers composition properties and pinned identities, not decoded
media pixels, audio playback, or rendered verification. Local preflight reports
remain useful for import plans, but the saved revision comparison is the exact
postpublication change view. Integer `--from-version` / `--to-version` address
the separate legacy shot/reference snapshot reader and cannot be mixed with
canonical revision IDs.
The receipt's `next_actions` include unpinned `timelines show` and
`timelines visualize --mode inputs` commands. Both commands support
`--revision-id`; append `--revision-id <summary.new_head>` to inspect the exact
published revision even after the head advances. Without that selector, normal
working-copy/current-head selection applies. Input visualization is a placement
diagram. Saving does not verify composed pixels or audio playback, and these
suggestions do not start a render.

## The control map

The normal inspection loop is deliberately short:

```text
timelines show       authored structure and timing
        ↓
timelines visualize  composed pixels, with synchronized input lanes
        ↓
checkout             detached, editable JSON at the current head
        ↓
check                local validation and a readable diff
        ↓
publish              one parent compare-and-swap publication
```

Both inspection commands print the next useful command in human output and
return the complete set of copyable commands in `data.navigation` (or
`outputs.navigation` for a render-backed result). The visual route retains
`exact_frame`, sampling, range, resolution, lane, and inspection commands in
that structured object, so a compact terminal footer does not hide detail.
The edit guide and helper catalog below are the stable route for changes.

## Where to edit

In these paths, `work` is the JSON document, `row` is one member of
`work["placements"]`, and `shot = work["shots"][row["shot_id"]]`.

| Change | Authoritative field |
| --- | --- |
| Authored shot order | Order of `work["placements"]`; chronology also depends on starts |
| Shot start | Normally `row["placement"]["start_ms"]`; existing `row["at_ms"]` takes precedence, so preserve the opened convention |
| Shot window duration | `row["duration_ms"]` at the row level, **not** inside `placement` |
| Shot display name | `shot["payload"]["name"]`; the current review/name projection also reads `shot["payload"]["metadata"]["name"]`, so update that field when present and check the selected UI reader |
| Internal picture/audio/layers | `shot["internal_timeline"]["clips"]` and `["tracks"]` |
| Internal clip timing | Clip `at`, `from`, `to`, `hold` in seconds; `speed` changes visible duration |
| Selected media | Clip `asset` (or its existing selector) resolved by internal timeline `registry`; mirrored shot items' `media_id` must resolve to the same managed object |
| Parent overlays/effects | Existing `work["parent"]["clips"]` and parent config/registry fields |
| Spoken script | Registered shot's `voiceover_script` descriptor in `payload.text_bindings`, registered via `client.shots` / `timelines shots text`, then pinned by parent publication |

Script text and playable voiceover audio are separate: changing the binding does
not synthesize speech, and changing an audio clip does not update the text.
Read `timelines script` to inspect the selected composition's pinned narration;
project text lists include reusable unplaced shots and are not timeline script reads.
Associate bindings with the
registered shot, not a temporary authoring ID. See the skill's narration commands.

Preserve `base_parent`, `base_parent_payload`, `base_placements`, `source_mapping`,
and per-shot base/source fields. They are checkout provenance, not editable
content. Let compilation allocate revisions, digests, occurrence revision pins,
and the dependency manifest. `parent.occurrences` is derived from `placements`.
Keep unrelated and unknown payload fields for a lossless round trip. State the
ripple/quantization policy when retiming; changing a duration does not implicitly
move later placements or extend parent overlays.

## Add or replace local media

Local paths are temporary inputs in the checkout document. To replace a selected
image, video, or audio asset, add `local_path` to its existing registry entry:

```python
shot = work["shots"][row["shot_id"]]
shot["internal_timeline"]["registry"]["assets"]["picture"]["local_path"] = "media/new-picture.png"
shot["internal_timeline"]["registry"]["assets"]["voiceover"]["local_path"] = "media/new-voiceover.wav"
```

For a new clip, create a registry entry with `local_path` and select that asset
key, or use a local file path directly as the clip's `asset`/`asset_id` selector.
New or changed `file`, `path`, and `src` fields in registry entries are also
accepted as local inputs. Relative paths resolve from the checkout JSON's
directory; absolute paths and local `file://` URLs are accepted. HTTP URLs are
not downloaded. Unchanged managed registry filenames remain presentation data.

The public SDK operations `plan_authoring_media(candidate, base_directory=...)`
and `import_authoring_media(plan, transport)` implement this preparation. Import
uses `WorkspaceClient.import_project_media`; it does not create a second media
store or claim a provider generated an upload. The existing Runtime import
publishes managed project bytes and an ordinary catalog Generation/original
Variant with `external_upload`/`imported` provenance. Audio imports keep audio
MIME/type and duration and omit visual dimensions.

The prepared document uses durable managed media IDs. Local registry locators
and stale selected-object hashes are removed; matching shot items, audio
bindings, and asset descriptors follow the selected replacement. Baseline
provenance and historical generation inputs remain pinned. If one old object
has multiple different new selections, update each mirror explicitly rather
than relying on an ambiguous replacement.

Preflight rejects missing/non-media/undecodable files and files over the existing
64 MiB Runtime limit before importing anything. Files are checked again before
the first import. Repeated references share an import; retries have stable
import keys and reuse matching imported catalog entries. A different imported
filename retains its distinct import provenance even when bytes match.

Imports and parent publication are separate existing transactions. If a later
import or parent CAS fails, the timeline is unchanged; completed imports remain
ordinary project catalog media. Keep the source JSON and retry after resolving
the failure. If the parent head advanced, check out a fresh document and reapply
the edit. The helper preserves the editable source JSON and writes the durable
rewrite separately.

## Review and recover

- **No render needed for saving:** refresh/open the same project and timeline in
  Reigh after publication. `timelines visualize --mode inputs` gives declared
  placement evidence without rendering. The default/`--mode auto` route uses a
  matching render when available and otherwise captures bounded composed frames
  through the canonical Remotion route. Use `--mode inputs` for render-free
  evidence; a frozen candidate JSON is not a visual preview.
- **Empty or wrong project list:** check the launcher/runtime selection before
  creating a new project or importing data.
- **Stale head:** check out a new file and reapply the intended edits. Do not
  overwrite the base head in the old file to force publication.
- **Missing media selector:** use the temporary local input fields above or
  import explicitly through `WorkspaceClient.import_project_media`, then use
  its returned managed identity. `media.import_file` alone manages project bytes
  and does not promise gallery visibility. Raw paths must pass through
  preparation before durable publication; do not guess managed IDs.
- **No-op diff:** an unchanged checkout now produces an empty authored diff.
  Placement timing and order remain meaningful changes.
- **Unsupported edit or unavailable API:** retain the local file and report the
  actual missing capability. Do not fall back to legacy document saves or
  manufacture a target descriptor that claims an edit capability.

## Editing helper catalog

The public `astrid.sdk.timeline_editing` helpers operate on the detached
candidate only. They do not publish, rewrite Runtime history, or bypass the
checkout's provenance fields. Use them for ordinary transformations, then run
`check` and inspect the diff before `publish`. The catalog is grouped by the
kind of edit it expresses and mirrors the module's `__all__` export list.

### Components and media

- `clone_candidate(candidate)` — make a lossless editable copy of a checkout.
- `add_track(container, ...)` — add a visual or audio track.
- `add_shot(candidate, ...)` — add a shot to the separate list-shaped
  candidate helper format (`candidate["shots"]` is a list). A checked-out
  authoring bundle stores `shots` as a mapping; use `add_authoring_shot(...)`
  for that shape instead.
- `place_media(container, media, track=..., start=..., end=...)` — place a
  managed media selector on a track. Import the file first; never put a local
  filesystem path in the checkout.
- `replace_media(container, clip_id, media, preserve_interval=True)` — swap a
  selected asset while retaining its timing by default.
- `duplicate(value, ...)` and `remove(container, item_id, ...)` — copy or
  remove a clip/row while preserving the surrounding document shape.

### Timing and frame policy

The standalone `timeline_editing` helpers `retime`, `retime_with_ripple`,
`sequence`, `align`, and `fit_duration` are deprecated compatibility APIs.
They have no internal callers and retain their existing behavior and
signatures. For timeline timing, use `Checkout.retime()`, `reflow()`,
`resolve()`, and `Checkout.sequence(...).fit(...)`. The time and frame
conversion helpers remain supported.

- `move(container, clip_id, track=..., start=..., end=...)` — move one clip.
- `retime(clip, start=..., end=..., ripple="none")` — change one interval;
  the default leaves later placements where they are.
- `retime_with_ripple(...)` — opt into a narrow ripple on one visual track;
  later clips on that same track move with the changed duration. Audio,
  voice, music, other tracks, and parent occurrences are not moved.
- `sequence(clips, start=..., durations=..., gap=...)` — lay out clips in
  order; `fit_duration(container, mode="extend")` updates the container to
  the resulting end.
- `frame_time(frame, fps)`, `source_to_timeline_time(...)`, and
  `timeline_to_source_time(...)` — convert between the admitted clocks.
- `quantize_time(seconds, fps, policy=...)` and
  `quantize_interval(...)` — make frame rounding explicit at the boundary.

For example, to move a visual clip and intentionally ripple later clips on its
same track:

```python
from astrid.sdk.timeline_editing import retime_with_ripple

retime_with_ripple(
    work["shots"]["shot-terminal"]["internal_timeline"],
    "clip-terminal",
    start=20.5,
    end=24.0,
    parent_duration="extend",
)
```

State the policy in the edit note. A duration change never implicitly moves
later placements, overlays, or audio.

### Layout and layers

- `align(clips, anchors, edge="start")` — align a set of clips to explicit
  anchors.
- `grid(rows, columns, gap=...)` — calculate repeatable positions for a
  multi-item visual layout.
- `reorder_layers(container, track_ids)` — change authored layer order without
  changing clip timing.

### Parent and authoring-shot structure

- `add_authoring_shot(...)`, `duplicate_authoring_shot(...)`, and
  `remove_authoring_shot(...)` — change the complete authoring-shot bundle.
- `move_occurrence_group(...)` — move a parent occurrence together with its
  linked internal structure.

These structural helpers are useful when a visual change spans a parent shot
and its internal timeline. They still produce one detached candidate; the
publication boundary remains `check` → `publish`, with the Runtime head
rechecked before the compare-and-swap.
