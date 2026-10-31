# Timeline document inspection and held checkout reference

Use this page for the complete closure's read/validation shape, media
inventory, and migration notes. A checkout is a detached snapshot, not a lock.
The write path is `tl.commit` from the [timeline skill](../../../timeline/docs/SKILL.md), a guarded
command commit through `TimelineSession`'s outbox; the broad
checkout publication described below remains held on this base.

The complete executable example is
[`../scripts/timeline_document.py`](../scripts/timeline_document.py). It defines
connection, discovery, immutable-closure, validation, and legacy-writer
variables; read it only when adapting migration tooling. It is not the general
timeline writer. `AstridClient.open_from_launcher` resolves
project/timeline names and pins the head. The public launcher receipt and
`WorkspaceClient` supply the revision reads and publication port that the
high-level client does not yet expose as a general checkout method. It never
constructs a pretend coordinator capability or accesses private client fields.

Run with Python from the environment where Astrid is installed. Keep the same
launcher configuration as Astrid App; for a checkout-backed installation this may
mean setting `ASTRID_RUNTIME_DATA_ROOT` to its already-configured data root.
Do not guess another root when the expected project is absent. Use project and
timeline **slugs or IDs**, not display titles.

```bash
python3 -m astrid timelines show <timeline> --project <project> --json
python3 -m astrid timelines edit move <timeline> --project <project> \
  --clip parent/clip:<id> --on 1.5 --idempotency-key move-42 --json
python3 -m astrid timelines edit track-label <timeline> --project <project> \
  --track parent/track:<id> --set "Dialogue" --idempotency-key label-42 --json
```

`show` reads the bounded canonical projection. The native commands open a
session, capture original guards, persist the exact request and retry identity
in its real outbox, and commit through Runtime. Use a fresh idempotency key for
each new intent; repeat the original request/key only to retry an ambiguous
flight. `--dry-run` validates without publishing.

The script's `checkout`/`check`/`publish` flow is retained for migration and
read-only closure inspection. Its broad parent-CAS writer, local prepared
document, and whole-candidate merge are not the current general write path.

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
Forward the exact scoped `next_actions`: use `timelines show` to view the timeline and
`timelines visualize --mode inputs` to visualize its declared layout. Both
commands read the **current head**, because these commands do not support an
exact revision option; retain `summary.new_head` as the publication identity.
Input visualization is a placement diagram. Saving does not verify composed
pixels or audio playback, and these suggestions do not start a render.

## The control map

The normal inspection/native-edit loop is deliberately short:

```text
timelines show       authored structure and timing
        ↓
timelines visualize  composed pixels, with synchronized input lanes (only on request)
        ↓
EditCommand          original guards and deterministic intent
        ↓
TimelineSession      real outbox and exact retry identity
        ↓
Runtime /v1 commit   one accepted command publication
```

Both inspection commands print the next useful command in human output and
return the complete set of copyable commands in `data.navigation` (or
`outputs.navigation` for a render-backed result). The visual route retains
`exact_frame`, sampling, range, resolution, lane, and inspection commands in
that structured object, so a compact terminal footer does not hide detail.
The edit guide and helper catalog below describe the detached shape for
inspection and migration. They are not permission to use a second writer.

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
ripple/quantization policy for timing changes; changing a duration does not
implicitly move later placements or extend parent overlays.

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
Variant with `external_upload`/`imported` provenance. Registry metadata follows
the editor shape: visual dimensions use a `resolution` string such as `1280x720`,
and `duration` is a number of seconds. Audio imports keep their MIME/type and
duration and omit `resolution`.

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

Imports and any legacy parent publication are separate existing transactions.
If a later import or held parent-CAS attempt fails, the timeline is unchanged;
completed imports remain ordinary project catalog media. Keep the source JSON
for migration/debugging and use a native command for supported edits. If the
parent head advanced, do not merge stale JSON or refresh its guards; inspect the
new head and capture a fresh command.

## Review and recover

- **No render needed for saving:** refresh/open the same project and timeline in
  Astrid App after publication. `timelines visualize --mode inputs` gives declared
  placement evidence without rendering. The default/`--mode auto` route uses a
  matching render when available and otherwise captures bounded composed frames
  through the canonical Remotion route; use it to check your visual edits
  ([how to look](how-to-look.md)). Use `--mode inputs` for render-free evidence; a frozen candidate JSON is not a visual preview.
- **Empty or wrong project list:** check the launcher/runtime selection before
  creating a new project or importing data.
- **Stale head:** inspect the new head and capture a fresh native command. Do
  not overwrite the base head in an old file or force a held publication.
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
checkout's provenance fields. Use them for inspection/migration preparation,
then run `check` and inspect the diff; publication requires a native command
where one exists. The catalog is grouped by the kind of edit it expresses and
mirrors the module's `__all__` export list.

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

- `move(container, clip_id, track=..., start=..., end=...)` — move one clip.
- Native command batches use `clip.trim` for source endpoints and `clip.move`
  for timeline placement. Record later same-track visual moves explicitly
  when a duration change is intended to ripple; audio, voice, music, other
  tracks, and parent occurrences remain unchanged unless separately commanded.
- `sequence(clips, start=..., durations=..., gap=...)` — lay out clips in
  order; `fit_duration(container, mode="extend")` updates the container to
  the resulting end.
- `frame_time(frame, fps)`, `source_to_timeline_time(...)`, and
  `timeline_to_source_time(...)` — convert between the admitted clocks.
- `quantize_time(seconds, fps, policy=...)` and
  `quantize_interval(...)` — make frame rounding explicit at the boundary.

State the policy in the command-group rationale. A duration change never
implicitly moves later placements, overlays, or audio.

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
