# Timeline authoring capabilities

`timelines show` and `client.timelines.open_composition` are read-only,
revision-pinned projections. Native edits use registered `EditCommand`s through
`TimelineSession`; the session captures original read/expect guards once and
keeps the exact request, retry identity, and newer local commands in its real
outbox. Runtime accepts the batch through the normal `/v1` commit route.

```python
from astrid.sdk.timeline.sync import TimelineSession

inspection = client.timelines.open_composition(project_id, timeline_id)
assert inspection.ok

session = TimelineSession.open(project_id, timeline_id, client=client)
result = session.move("parent/clip:opening", 1.5, idempotency_key="move-42")
publication = result["receipt"]["receipt_id"]
```

`session.commit(command)` and `session.commit_many(commands)` accept registered
commands. `session.trim(select, in_s=..., out_s=...)` is the supported literal
source-trim convenience. `session.undo([publication])` sends a new guarded
inverse; it never rewinds accepted history. Use a fresh idempotency key for a
new intent. Repeating the original call with its original key retries the
persisted flight rather than recapturing guards from a newer head.

## Native coverage

The current command registry is intentionally narrower than the detached
authoring-bundle field model:

| Native operation | Current use |
| --- | --- |
| `clip.move` | Move a scoped parent/occurrence clip with explicit timing intent. |
| `clip.trim` | Change literal source endpoints without recapturing the base. |
| `track.set` | Set or clear a registered track field. |
| `track.create` | Create a track with a stable caller-supplied identity. |
| Registered M2 commands | Use only operations listed by the installed command registry/help. |

Commands record writes (including unchanged values), reads, selections, binding
claims, and created IDs. Created identities must remain stable across a retry;
the Runtime receipt is the accepted result. Ripple is explicit: group a trim
with recorded moves for later visual siblings and leave unrelated audio/voice/
music rows untouched. Half-open timing and source/timeline unit rules remain
those of the shared timeline schema.

## Producer proposals

Producers store immutable artifacts and emit a settled proposal; they do not
publish a timeline directly. The requesting client queues the exact settled
proposal and flushes it through the same outbox:

```python
session.queue_proposal(settled_proposal)
result = session.flush(idempotency_key="proposal-42", via="producer")
```

The proposal's admitted target, requester/attempt pin, rationale, original
guards, and command identities are preserved. A changed proposal, target, or
identity is refused. An artifact, proposal digest, or dry run is not a
publication receipt; retain the Runtime receipt returned by `flush`.

## Held authoring-bundle capabilities

The detached authoring-bundle model and its field contract remain useful for
read/validation, media inspection, renderer preparation, and migration work.
Its fields include parent configuration, placements, shot payload metadata,
internal tracks/clips/effects, media selectors, and opaque extension data. Those
objects are not a second timeline format, but broad mutations such as arbitrary
metadata, occurrence transforms, media replacement, add/remove/reorder batches,
and whole-bundle publication are not all command-backed on this base.

Do not call `publish_authoring_candidate`, `bound.publish`, or a broad
`Checkout.publish` as the general writer. Keep an unsupported edit as a local
draft and report the exact missing command, or use a native command when one is
available. The cut-sheet adapter is similarly mixed: `apply --native` is
supported only for covered operations; its remaining three-way merge and
detached-draft publication stay held.

Media import/transcoding remains an explicit Runtime media operation. A missing
managed selector, duplicate identity, unsupported renderer capability, or
unsupported command must fail visibly before a commit. Read-only media
inventory/source access does not mutate the timeline and does not establish a
publication.
