# Code-driven timeline editing

The default flow is **inspect → edit → validate → save**. Inspect the supplied
timeline and source media as needed, edit a detached candidate, check its diff,
and publish through Runtime's compare-and-swap boundary. Iterate and save again
when useful. Rendering, visualization, and reopening the saved closure are
optional checks unless the request needs them; a normal edit does not require
a render or an exact-head readback ceremony.

If the user says **“show me the preview, then save”**, preserve that order:
render the unpublished candidate, inspect/deliver its actual output, then save
that same candidate. Freezing candidate JSON is not a visual preview. If the
required preview fails or is unavailable, report that and leave the candidate
unpublished. A render after saving cannot satisfy a preview-before-save request.

## Shared timeline and shot contracts

`@banodoco/timeline-schema` owns `TimelineConfig` and timeline clip semantics.
Workspace Runtime's `contract/schemas/shot-composition.json` owns the outer
shot publication envelope (revision identity, head/CAS, dependencies, and
occurrences) and references the generated canonical config schema for nested
timelines. The Runtime schema also marks its managed-media registry as a
Runtime-owned extension. Consumers project these contracts into their local
views; they do not define a competing `TimelineConfig`.

Shot audio is optional. Child timeline audio clips and their bindings are the
playback authority; a legacy aggregate `audio` descriptor may be read when
present, but is neither required nor synthesized. Shot payloads remain open to
unknown fields so app-specific data survives a lossless read/edit/publication
round trip. New writes use top-level `name`; readers may fall back through
legacy provenance/metadata names and finally the stable shot identity. Media
kind comes from Runtime's managed-media metadata and stays `unknown` when that
identity is missing or unrecognized; a missing kind never implies `image`.

Visible clip duration is the selected source duration divided once by positive
`speed`: `hold`, transport `duration_ms`, or source trim `to - from` supplies
the source duration. Parent occurrence `duration_ms` caps the resulting visible
interval. Source trims remain in source units, and the renderer owns final frame
rounding against the shared conformance vectors.

The Astrid App editor reads the exact canonical Runtime head. Empty canonical
compositions remain canonical; a failed read shows an error and never routes to
legacy shot groups. Legacy mode is explicit and is selected only when the
canonical adapter is absent. Retry refreshes the pinned Runtime snapshot while
preserving the separate local draft, and publication uses expected-head CAS.

`open_composition` is a read-only inspection projection. Editing uses a
separate detached bundle opened from an exact parent/shot/internal-timeline
revision closure; that bundle is then validated and published as
one complete candidate through the existing Runtime writer.

## Extension data and repeatable passes

The shared schema provides open maps for application data, so a timeline can
carry a small processing ledger without a parallel document or a new schema.
Use the narrowest scope that matches the meaning of the value:

- timeline-wide structure: `candidate["parent"]["config"]["app"]["structure"]`
- one shot/revision: `candidate["shots"][shot_id]["payload"]["metadata"]`
- one occurrence: `candidate["placements"][i]["provenance"]`
- one internal clip or effect: its `app` or `params` map

`config.app.structure` is the schema's intentional timeline extension point. A
direct `config.structure` field is rejected by `TimelineConfig`. Shot payloads
are open so application metadata survives a lossless read/edit/publication
round trip, while the common timeline schema still governs known structure and
timing fields. The editor may not display arbitrary keys until a UI projection
is added. Keep agent-owned values under a namespace such as
`metadata["delivery"]`; if one shot revision is reused by two placements, put
different delivery state under each placement's `provenance`.

### Example: inspect every placement and track delivery state

`timelines show` (or `client.timelines.open_composition`) is the canonical
current-head read. It gives the ordered occurrence IDs, names, timing, tracks,
and pinned head. The read projection is not editable. Use a coordinator-issued
authoring target for the same project, timeline, and head, then open that exact
immutable closure as a detached bundle:

```python
shown = client.timelines.open_composition(project_id, timeline_ref, detail=True)
assert shown.ok
inspection = shown.data
assert target["head_revision_id"] == inspection["summary"]["head_revision_id"]

bound = client.open_authoring_target(target)
work = bound.open()
work["parent"].setdefault("config", {}).setdefault("app", {}).setdefault(
    "structure", {}
)["delivery_pass"] = {"version": 1, "status": "queued"}

for index, placement in enumerate(work["placements"]):
    shot = work["shots"][placement["shot_id"]]
    clips = shot["internal_timeline"].get("clips", [])
    shot["payload"].setdefault("metadata", {})["delivery"] = {
        "instructions": "Match the approved reference and preserve the cut.",
        "status": "queued",
        "source_head": inspection["summary"]["head_revision_id"],
        "clip_count": len(clips),
    }
    placement.setdefault("provenance", {})["delivery_index"] = index

bound.validate(work)
diff = bound.diff(work)       # inspect changed paths before saving
frozen = bound.preview(work)  # exact candidate JSON; no pixels
receipt = bound.publish(work, idempotency_key=frozen["candidate_digest"])
```

This is one candidate and one publication, so the status ledger and timeline
change share one head. If publication reports a stale head, discard the
detached candidate, run `timelines show` again, and rerun the deterministic
pass. Do not merge stale JSON or publish one shot at a time. The exact SDK
target is deliberately separate from the read projection: it carries the
Runtime endpoint, edit capability, project/timeline identity, and pinned head.

### Example: report or edit every internal item

Use the same pinned bundle when the question is about clips, tracks, effects,
or item-level metadata. The report is read-only until the `app` label below is
retained in the candidate:

```python
report = []
for placement in work["placements"]:
    shot = work["shots"][placement["shot_id"]]
    for clip in shot["internal_timeline"].get("clips", []):
        report.append({
            "occurrence_id": placement["occurrence_id"],
            "clip_id": clip["id"],
            "track": clip.get("track"),
            "at": clip.get("at"),
            "from": clip.get("from"),
            "to": clip.get("to"),
        })
        clip.setdefault("app", {})["report_label"] = placement["occurrence_id"]

# For duration changes, call retime/quantize with an explicit frame/ripple
# policy rather than inventing renderer-specific timing keys.
bound.validate(work)
diff = bound.diff(work)
frozen = bound.preview(work)
receipt = bound.publish(work, idempotency_key=frozen["candidate_digest"])
```

The report uses occurrence IDs for navigation and the bundle's authoring shot
IDs for mutation. Stable Runtime IDs remain immutable; compilation and CAS
publication allocate or reuse revisions as appropriate. `preview` freezes
candidate JSON but does not render pixels; use the managed Remotion preview
path when visual proof is required. These extension maps do not automatically
appear in `timelines show` or the browser: exposing them to a UI requires an
explicit Runtime inspection field and regenerated consumers.

### Renderer and skill boundary

Remotion is the visual proof path for a frozen candidate, not a second timeline
authority. Keep effects in the admitted internal timeline `effects` and clip
`app`/`params` fields, then call `render_authoring_candidate_preview` when
composed pixels are required. Production orchestrators remain in
`video_editing`; canonical timeline authoring, visualization, effects, and
Remotion rendering remain in `timeline_editing`. The source directory and
runtime capability IDs stay `rendering` for compatibility, so the safe rename
is the skill/frontmatter and routing text rather than a pack-ID migration. After
editing the source skill, run `python3 -m astrid.skills sync --all` followed by
`python3 -m astrid.skills sync --check --json` to refresh and verify the root
gateway and installed views.

## Read the live timeline first

Use the connected Runtime for discovery and readback. `timelines show` is the
native structural/text operation. For declared visual placement, use its
sibling native Runtime operation:

```bash
python3 -m astrid projects current --json
python3 -m astrid timelines show [--project <project>] [<timeline>] --json
python3 -m astrid timelines visualize [<timeline>] [--project <project>] \
  --mode inputs --format md --format png --occurrence <occurrence-id> --json
```

Explicit scope wins. If it is omitted, the Runtime uses the workspace current
project and that project's `metadata.default_timeline_id`; it does not infer a
timeline from names or list order. A missing selection is an actionable error.

Pin the head returned by `show`, then carry the exact `occurrence_id` returned
by the live response into any input view or later focused inspection. Do not replace
it with a shot name, ordinal, fixture alias, seed-map entry, or a nearby
occurrence. The input view describes declared placement and timing; it does
not prove source pixels or a composited render.

`timelines visualize` is the one visual-inspection operation. Its default
`auto` mode reuses a fresh matching render or captures the requested composed
frames from the pinned composition when no render exists. Use `--mode inputs`
for declared inputs without any renderer work. `--mode composed` requests
composed pixels; `--frame N` selects one exact frame, and `--revision-id ID`
keeps historical/current authority explicit. The returned evidence reports
whether frames were reused, freshly captured, or served from cache. Agent conclusions must come from live
Runtime responses and returned artifacts, never from fixture/evaluator JSON,
baseline exports, or prior result files.

For a supplied target and credential file, the complete public SDK route is
below. Save it as `edit_target.py` and pass `target.json`, the supplied token
path, an edit JSON object such as `{"occurrence-id": 1200}` (new `start_ms`
values), and the credential's actor ID (the disposable owner token uses
`owner`). This example saves an edit without rendering. Keep SDK work in the
same connected environment as the working shell command: a persistent Python
evaluator may have a different environment and working directory. Importing
the SDK does not connect it. Use the supplied connection explicitly as below
if it is not already available; never guess credentials or inspect private
Runtime storage to recover them.

```python
import json
import sys
from pathlib import Path

from astrid.sdk.client import AstridClient
from astrid.sdk.workspace_client import PROTOCOL

target = json.loads(Path(sys.argv[1]).read_text())
edits = json.loads(Path(sys.argv[3]).read_text())
assert edits and all(isinstance(value, int) for value in edits.values())
client = AstridClient.open(
    endpoint=target["endpoint"], credential=Path(sys.argv[2]),
    realm_id=target["realm_id"], actor_id=sys.argv[4],
    client_name="astrid-timeline-editor", client_version="1",
    protocol_version=PROTOCOL,
)
bound = client.open_authoring_target(target)
work = bound.open()  # detached parent/shot/internal closure at target's exact head
placements = {row["occurrence_id"]: row for row in work["placements"]}
assert edits.keys() <= placements.keys()
for occurrence_id, start_ms in edits.items():
    placements[occurrence_id]["placement"]["start_ms"] = start_ms
assert all(placements[key]["placement"]["start_ms"] == value
           for key, value in edits.items())
bound.validate(work)
diff = bound.diff(work)
assert diff["changed"]  # inspect the full diff and assert all requested behavior
frozen = bound.preview(work)  # frozen JSON and digest; no rendered media
receipt = bound.publish(work, idempotency_key=frozen["candidate_digest"])
new_head = receipt["publication"]["new_head"]
print({"new_head": new_head,
       "candidate_digest": receipt["candidate_digest"]})
```

`bound.open()` returns a plain candidate: each `placements` row has its
`occurrence_id` and `shot_id` at the top level, with timing under
`row["placement"]["start_ms"]` / `row["placement"]["duration_ms"]`.
Do not add flat timing fields or mutate the derived `parent.occurrences`.
`shots[shot_id]["payload"]` and
`shots[shot_id]["internal_timeline"]` hold the selected shot and its nested
clips. `bound.validate`, `bound.diff`, and `bound.preview` return plain data;
the optional rendered preview returns an `InvocationResult` with `.ok`,
`.error`, and `.run_id`. Retain the publication receipt to identify the saved
state. If a later check needs readback, reopen its returned `new_head`; do not
substitute a baseline snapshot. Report a successful save separately from any
claim about rendered pixels or sound.

```python
from astrid.sdk import (
    add_authoring_shot, add_track, place_media, sequence,
    open_authoring_bundle, validate_authoring_candidate,
    preview_authoring_candidate, publish_authoring_candidate,
)

# Inspection only: this does not return an editable candidate.
inspection = client.timelines.open_composition(project_id, timeline_id)

# These three inputs are the exact pinned closure read from Runtime.
work = open_authoring_bundle(
    pinned_parent, shot_revisions=pinned_shots,
    internal_timeline_revisions=pinned_internal_timelines,
)
shot = add_authoring_shot(
    work,
    name="Brightness montage",
    occurrence_id="brightness-montage",
    start_ms=0,
)
track = add_track(shot, kind="visual")
images = sorted(imported_media, key=lambda item: (brightness[item["id"]], item["id"]))
clips = [
    place_media(shot, image["id"], track=track, start=0, end=0.1, fit="cover")
    for image in images
]
sequence(clips, start=0, durations=[0.1] * len(clips))
# The child sequence does not implicitly resize its parent occurrence.
placement = next(row for row in work["placements"]
                 if row["occurrence_id"] == "brightness-montage")
placement["placement"]["duration_ms"] = round(len(clips) * 0.1 * 1000)

validate_authoring_candidate(work)
frozen = preview_authoring_candidate(work)  # exact candidate artifact; no pixels rendered
receipt = publish_authoring_candidate(work, runtime_writer, idempotency_key=edit_run_id)
```

Important defaults are explicit:

- Clip intervals are half-open `[start, end)` in the containing timeline.
- Use `quantize_interval(start, end, fps, policy=...)` when boundaries are not exactly representable; it reports requested/applied frames and rejects collapsed intervals.
- Absolute placement does not move neighbours. Ripple is never implicit.
- If shifting following visual clips is intended, call `retime_with_ripple` with
  `scope="track"` and an explicit `parent_duration` policy. It never ripples
  audio, voice, music, or another track.
- `duplicate` allocates fresh editable identities and remaps internal references; immutable media can remain shared.
- `replace_media` selects the supplied asset and preserves the existing interval unless instructed otherwise.
- `reorder_layers` changes stacking order; `sequence` changes chronology.

| Helper | Effect |
| --- | --- |
| `place_media` | Adds one admitted media reference with `at`, source `from`/`to`, and optional `speed`. |
| `move` / `retime` | Changes timeline placement/length while retaining source origin and speed. |
| `sequence` / `align` | Arranges supplied clips without affecting unrelated clips. |
| `retime_with_ripple` | Shifts later visual clips on the same track under an explicit duration policy. |
| `fit_duration` | Sets or extends the container duration to cover its clips. |
| `quantize_interval` | Reports exact requested and frame-applied half-open boundaries. |
| `duplicate` / `replace_media` / `remove` | Copies identity, changes selected media, or removes an authored object. |

The editing helpers do not select a “latest” asset, download the whole library,
or publish individual operations. Media import/list/open use the existing
runtime media services. Source viewing and composed preview are separate,
read-only actions. Unsupported renderer capabilities and missing assets must be
reported before commit.

## CLI working-copy checks and journal

The thin timeline CLI exposes the existing detached working copy through this
read/edit/check/publish loop:

```bash
python3 -m astrid timelines status <timeline> --project <project>
python3 -m astrid timelines checkout <timeline> --project <project>
python3 -m astrid timelines edit <timeline> --project <project> --replace-line vo-1 --take vo/vo-1.wav --words vo/vo-1.words.json
python3 -m astrid timelines edit <timeline> --project <project> --insert-line vo-2 --after vo-1 --take vo/vo-2.wav --words vo/vo-2.words.json
python3 -m astrid timelines edit <timeline> --project <project> --from-script script.json
python3 -m astrid timelines check <timeline> --project <project>
python3 -m astrid timelines publish <timeline> --project <project> --idempotency-key edit-42
```

`status` reads the current published head and, if present, the selected
working copy's base, changed paths, and validation result. It does not create a
working copy or render. There is no passive timeline-scoped latest-render
reader in this CLI, so `status` does not claim render state. A working copy
based on an older head is reported with `head_moved: true` and should be
reconciled before publishing.

`checkout --fresh` refreshes the selected working copy from the current head.
If that copy has edits, the CLI refuses to replace it; choose a new name with
`--draft recovered --fresh` to keep the old draft available. `edit` can also
apply `--replace-line`, `--insert-line`, or a JSON `--from-script` using the
existing SDK operations and local take/word-timing files. Repeat
`--gap-after LINE=SECONDS` to declare multiple line gaps.

`publish` requires an existing working copy, calls the SDK once, and uses its
three-way merge and conflict behavior. It generates an idempotency key when
one is not supplied; `--force` is an explicit choice to prefer this draft in
overlapping edits. If the structure is published but narration binding fails,
the command exits with a partial-publication error and preserves the full
receipt in JSON. It never retries narration automatically; reconcile the
reported state before another publish attempt.

```bash
python3 -m astrid timelines check <timeline> --project <project> --sweep
python3 -m astrid timelines edit <timeline> --project <project> --clip <clip> --nudge-frames 1 --sweep
python3 -m astrid timelines log <timeline> --project <project> --verify --json
```

`check --sweep` validates a saved working copy and compares bounded samples across every cut. Sweep status is 0 for expected results, 1 for unexpected picture changes, and 2 when capture was not made; a validation failure returns 1. Without a saved working copy, `check` prints a checkout hint and does not sweep.

A changed edit saves the working copy before sweeping. The sweep runs after a reflow, when more than three cuts changed, or with `--sweep`. `--no-sweep` suppresses it, including when both flags are supplied. An unchanged edit reports no change, returns 3, and does not sweep. These bounded samples do not prove every-frame or audio identity.

`log --verify` checks the journal hash chain. A broken chain returns 1; JSON reports `error.code: validation_error`. A missing journal is reported as “no journal yet” and returns 0. Journal queries accept `--since`, `--actor`, `--op`, `--cut`, `--clip`, and `--outcome`; a query validation error is printed to stderr and returns 2.

## Render an unpublished candidate

Use this optional branch when a composed preview helps or is explicitly
requested. For preview-before-save, insert it before `bound.publish` in the
example above and inspect/deliver the returned output before publishing the
unchanged candidate. Runtime must have `rendering.render` available.

`preview_authoring_candidate(work)` freezes the complete candidate, its base
parent head, deterministic candidate digest, and proposed publication. For a
composed video preview, use the connected SDK client:

```python
from astrid.sdk import preview_authoring_candidate, render_authoring_candidate_preview

frozen = preview_authoring_candidate(work)
render = render_authoring_candidate_preview(
    frozen, client, project="my-project", timeline_ref="main", wait=True,
)
assert render.ok, render.error
# Inspect/deliver this run's output before saving if the user requested that order.
```

This invokes the ordinary `rendering.render` managed run. Admission rereads the
base head and any reused immutable child revisions from Runtime, projects the
frozen candidate without publishing it, checks project-owned media, and
records the run receipt. The render authority is labelled `Unpublished
candidate preview` and includes the exact base head, candidate digest,
publication digest, and proposed parent revision ID. Editing `work` after the
preview was frozen cannot alter that run. If the parent head advances before
admission, the preview is rejected as stale.

## Opening sources and returned artifacts

Keep these identities distinct: a clip's `asset`/`asset_id` is a timeline-local
registry alias; a managed media ID identifies a project media record; a
`source_object_id` identifies managed bytes; an artifact handle identifies a
returned view or run output. Resolve the selected clip through its owning
registry or returned source metadata. Never pass a display alias to an API
that asks for a managed ID, or construct an artifact URL from an alias/digest.

For a returned, non-null source object, the connected SDK can retrieve bytes
with `client.media.read_bytes(source_object_id)`. Read/open only the selected
source needed for the task. Media metadata alone does not prove that its bytes
were viewed or played. A missing source object is an explicit access gap.

CLI responses can use `ok/data/error/receipt` envelopes, while bound authoring
helpers return plain candidate/receipt data. Check success and inspect the
documented payload level before selecting nested fields; do not assume every
operation returns the same top-level shape. Use returned verified local paths
with the host's file/image/audio tools when available. An `artifact://` value
is not a filesystem path, and a host's numeric artifact reader may not accept
Astrid's digest-based artifact handle. If no compatible opening action or
materialized path is returned, report that access limitation; do not invent a
URL or claim to have inspected the artifact.
