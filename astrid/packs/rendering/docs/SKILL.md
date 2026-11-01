---
name: rendering
description: >
  Author, inspect, edit, preview, render, and open runtime-owned Astrid
  timelines, including Remotion output, explorable Three.js worlds, and
  render-time animation, transition, and effect elements.
---

# Timeline editing

This is the canonical skill for runtime-owned timeline work: inspect → edit →
validate → save, with Remotion rendering and effect/evidence workflows when
needed. The broader [video editing skill](../../video_editing/docs/SKILL.md)
owns production orchestrators such as hype edits, talks, thumbnails, and logo
grids; it is not a second timeline-authoring route. In a checkout the source is
`astrid/packs/rendering/docs/SKILL.md`; in an installed skill view it is
`packs/rendering/SKILL.md`.

Use this skill to render, inspect render/input evidence, and open timeline
output for a candidate or published state. Rendering is an optional downstream
evidence action within the same timeline route, not a second timeline authority.
When the request also needs
new generated media (for example Foley audio), use [creative work](../../_core/docs/creative-work/SKILL.md)
to find its generation capability, then return here to assemble the result.

## Timeline transfer

Use `rendering.timeline_export_plan` to read a saved timeline and receive a task
artifact containing the complete export plan. Fetch the `plan` task output through
the existing task managed-output API; the action does not assemble an archive or
create project objects.

Use `rendering.timeline_prepare_import` in two passes. First pass `manifest` (or
the `manifest_file` fallback) with `check_only=true` before uploading any media.
After successful compatibility checks and client-side uploads, pass the manifest
and a JSON `payload` file containing `{ "timeline": ..., "transcripts": ... }`
to receive a `prepared` commit-body artifact. The prepared body is still only a
proposal; the client owns upload/commit and any target-project object management.

These actions do not publish timelines, write project documents, or associate
task artifacts with a project.

## Worlds (Three.js live scenes)

When the task concerns native Three.js scene code, a world, or a
`haus.astrid.liveScene` clip, first confirm that the Three.js Live Editor
extension and its operations are active; installed documentation alone does
not make the capability available.

The contract: **worlds are always explorable; shots live within them.** A
world is one self-contained HTML entry implementing `window.astridScene`. It
declares `explore` (required: where the free view starts and may go), its
`events` (in world time) and its `shots` (named source-time ranges, each
with an optional `worldStart`). Its `render` draws the authored shot for a
`sourceTime`, or exactly the World Explorer's `view` at `worldTime`. Before
creating, converting or editing a world, read
[Worlds: authoring explorable live scenes](references/worlds-authoring.md). It
covers the four layers (world constructor → world state from world time,
including events → shots → free view), holding up from any viewpoint,
software-GL performance, the package contract, admission, and the
read/exact-edit/publish loop. Start from the
[world template](templates/world-template.ts).

Ordinary clip placement, trim, repetition, playback rate and audio remain on
this general timeline route. Events and shots live in the world's source.
Maple Hollow is a continuous take (one implicit shot) and does not declare
`explore` yet. This route does not add a retiming engine, a scene-audio
subsystem, a generic ZIP importer or a second timeline authority.

Treat the connected workspace runtime as the sole authority for projects,
timeline documents, versions, media objects, tasks, runs, and render outputs.
Use the public CLI or SDK; do not edit a checkout database, event log, CAS tree,
or generated run directory. Read current help before using a new option:

```bash
python3 -m astrid --help
python3 -m astrid timelines --help
```

## Start a new timeline

Create timelines through the supported V3 product command. It calls the
Runtime-backed `RemoteTimelines.create_empty` route, which creates the
identity shell and publishes the first empty canonical head in one
caller-keyed operation:

```bash
python3 -m astrid timelines create <timeline-id> \
  --project <project> --canvas 1920x1080 --fps 30 \
  --idempotency-key create-timeline-01 --json
```

The canvas and frame rate are stored in the parent-composition config at
`theme_overrides.visual.canvas`. Repeating the command with the same caller
key is the supported retry path. The retired whole-document `timelines create`
route and local timeline files are not authorities.

## Point the user to the editor; render only on request

After any timeline edit, publish, then give the user the editor link and the
times to look at. Do not render, open a fresh render, or capture composed
frames as a step of the work. Render, or run `timelines visualize` for composed
frames, only when the user asks for a render, an exported file, or frames.

To check your own edit without rendering, read timing and structure with
`timelines show --as script` and declared placement with
`timelines visualize --mode inputs`.

Build the link from two ids:

```text
https://astrid.haus/tools/video-editor?project=<project-id>&timeline=<timeline-id>
```

- `<project-id>` is `project_id` from `projects list --json` (the 32-hex id,
  such as `811a657af5fe43e4b2b48a670de1f3e8`).
- `<timeline-id>` is `timeline_id` from `timelines list --project <project>
  --json`. Named timelines use their slug (`almost-ready-no-first-second-v2`);
  others use a ULID. The editor also resolves a project slug, and a timeline
  slug or ULID, to these ids, but use the ids.

Each published revision shows in the editor at once.

## Visualize (on request)

The structural view and `--mode inputs` are render-free. Composed frames are
for when the user asks for frames:

```bash
python3 -m astrid timelines show <timeline> --project <project> --json
python3 -m astrid timelines visualize <timeline> --project <project> \
  --mode inputs --format md --format png --json
```

When the user asks for frames, look at the film the way they mean to, one page each (pass a window and the view follows it):

```bash
python3 -m astrid timelines visualize <timeline> --project <project>                          # overview: a tile per cut
python3 -m astrid timelines visualize <timeline> --project <project> --range 5..10            # scan: 2 fps, 8 per row
python3 -m astrid timelines visualize <timeline> --project <project> --preset motion --at 5.3  # big frames, 1 s, every 2nd frame
python3 -m astrid timelines visualize <timeline> --project <project> --preset beat --range 5..10  # a frame per word/hit/sfx
python3 -m astrid timelines visualize <timeline> --project <project> --at 7.2                 # one frame, 1280x720
python3 -m astrid timelines visualize <timeline> --project <project> --cut 17                 # one cut's motion sheet
python3 -m astrid timelines visualize <timeline> --project <project> --highlight c41.tool-16  # outline one layer
python3 -m astrid timelines visualize <timeline> --project <project> --plan                   # what it would show, no capture
```

`--at`, `--range`, `--cut` and `--highlight` take addresses (`c30`, `c41.mink`, a spoken word),
read from the working copy unless `--published`. With a working copy and no window, the default
is the cuts you changed (`--every-cut` for all of them, `--compare published` for before/after).
`--view diff --before-revision-id A --after-revision-id B` (or `--from A --to B`, `--edited 17`)
compares two saved revisions. `--list-layers` lists the layers you can add with `--layer`; to
write one, read [visualize-extend.md](references/visualize-extend.md). A contact view of a render
(`--render-run RUN --render-every 5`) also prints its loudness and silences.

`--mode inputs` is render-free and answers where admitted media and authored
placements are declared. For composed pixels, use the same pinned timeline
with `timelines visualize` in its default/composed mode. The V3 render-prep
contract is the public `timelines.visualize`/`open_composition` SDK surface
plus `rendering.render` with `timeline_ref`; there is intentionally no
`astrid/sdk/timeline_rendering.py` facade to restore.

## Render and validate output

Only when the user asks for a render or an exported file. When one is required, use the supported runtime-owned route:

```bash
python3 -m astrid timelines render <timeline> --project <project> \
  --backend rendering.remotion --wait --json
```

Inspect the returned managed output and receipt through the Runtime. For a
local path that has been explicitly materialized for validation, retain the
standalone black-frame checker:

```bash
python3 tools/check_black_frames.py --video <render.mp4> \
  --timeline <timeline.json> --assets <assets.json>
```

`tools/render_and_check.py` is retired: it targeted the removed executor
runner's `rendering.render` wrapper and must not be revived. The checker
does not render, publish, or bypass Runtime custody; it only validates the
already-produced video.

Rendering prerequisites are the configured Astrid launcher/runtime, an
admitted `rendering.render` capability, and a qualified renderer such as
`rendering.remotion` with its declaration-driven pack aliases and local
font/resource provenance. Do not install a browser or renderer from this
skill; missing host/native prerequisites must be reported by the owning Sol
lane.

## Edit an existing timeline as a JSON document

For the everyday edit loop (checkout, cut sheet, moments, check, publish), start with
[the editing front door](references/editing.md). This section is the low-level document route.

For “check it out, edit it in a text document, then check it back in”, use the
[complete checkout/edit/check-in recipe](references/document-checkout.md) and
its runnable [SDK example](scripts/timeline_document.py). Start with the
connected runtime's project and timeline slugs: export the complete pinned
composition, edit its JSON, validate and inspect the diff, then publish one
revision against the original head. The example defines every variable; no
pre-supplied `target`, `pinned_parent`, or `runtime_writer` is needed.
Temporary local media paths in the edited document are rejected by the
read-only check. Import new media first through the public media client, then
use its managed digest in the checkout before validating and publishing.
Image, video, and audio imports appear in the ordinary gallery; audio keeps its
own type and native playback controls. An unchanged checkout has an empty diff.

This bundle includes parent layers, shot placements, and internal clips.
Narration is read through `timelines script`, which resolves the selected composition's pinned text bindings in occurrence order. Rendering is optional:
a saved revision can be reviewed in Astrid App. The recipe includes the authoritative
field map, media/gallery distinction, and common recovery paths.

For ordinary candidate edits, use the [timeline editing helper catalog](references/document-checkout.md#editing-helper-catalog).
It groups media/track changes, timing and ripple operations, layout helpers,
and occurrence moves. Helpers mutate only the detached JSON candidate; the
same `check` → diff → `publish` boundary remains authoritative.

For repeatable draft narration synchronization, use `video_editing.sync_draft_voiceover` as documented in the [video editing skill](../../video_editing/docs/SKILL.md). It reads the timeline's pinned script, synthesizes through `generation.generate_speech`, and publishes one validated revision. Empty scripts are skipped and existing final or unadopted audio stays protected. For a one-off manual import, see the [placeholder voiceover recipe](references/placeholder-voiceover.md). Neither workflow needs a video render.

### CLI checks and journal

Use `timelines check <timeline> --project <project> --sweep` for bounded checks across every cut. Changed edits sweep after a reflow, more than three changed cuts, or explicit `--sweep`; `--no-sweep` suppresses the sweep, which captures composed frames; pass it unless the user asks for frames. A sweep returns 0 for expected results, 1 for unexpected picture changes, or 2 when capture was not made. An unchanged edit returns 3 without sweeping.

Use `timelines log <timeline> --project <project> --verify --json` to verify the journal hash chain. A broken chain returns 1 with `error.code: validation_error`; a missing journal returns 0. See the [timeline editing guide](../../../../docs/timeline-editing-guide.md) for filters and details.

For direct detached-draft editing, the public CLI uses the existing timeline
SDK checkout operations:

```bash
python3 -m astrid timelines status <timeline> --project <project>
python3 -m astrid timelines checkout <timeline> --project <project> --draft recovered --fresh
python3 -m astrid timelines edit <timeline> --project <project> --replace-line vo-03 --take vo/vo-03.wav --words vo/vo-03.words.json
python3 -m astrid timelines edit <timeline> --project <project> --insert-line vo-04 --after vo-03 --take vo/vo-04.wav --words vo/vo-04.words.json
python3 -m astrid timelines edit <timeline> --project <project> --from-script script.json --takes vo --gaps
python3 -m astrid timelines check <timeline> --project <project>
python3 -m astrid timelines publish <timeline> --project <project> -m "<what changed>" --idempotency-key edit-42
```

`status` reports the canonical head and an existing draft's base, edits,
validation, and whether its base head moved, without creating a draft or
rendering. A dirty selected draft is kept intact when `checkout --fresh`
refuses; use a new `--draft` name to start from the current head. `publish`
requires an existing draft and calls the SDK
once. If structure was published but narration binding is pending, preserve the
returned receipt and reconcile it explicitly; the CLI does not retry. There is
no passive timeline-scoped latest-render reader here, so `status` has no
`--render` option.


## Agent operating contract

The timeline is one canonical authoring composition. `timelines show` (and the
typed authoring-bundle opener) is the bounded structural/text view;
`timelines visualize` is the declared-input/composed-output visual view; and offline
`timelines inspect --manifest` is a bounded read-only view of an already
published evidence pack. These are sister commands over the same identity and
time model in the single `timelines` product family. Both `auto` and `composed`
capture the requested bounded frames from the exact pinned saved composition,
even when a matching successful MP4 exists. An explicit `--render-run` selects
the saved revision to inspect; its video is never the frame source. Explicit
`--mode inputs` never looks up renders and remains renderer-free. `--frame N`
requests one exact composed frame. Source decoding, waveforms, and filmstrips
are private implementation details of the same operation. Never invent a
second text-only timeline, treat a filmstrip as a new source of truth, or
switch to a mutable child document because it is easier to read.

The default composed surface keeps the rendered output and synchronized input
lanes together, so a sampled frame can be compared with the source placement
that produced it. Use `--hide inputs` for output-only review, or
`--mode inputs` for a renderer-free placement view. `--every SECONDS` and
`--every-frames N` control temporal sample spacing; `--resolution WIDTHxHEIGHT`
controls the pixels used for each sampled output image. The returned receipt
records the resolved components, cadence, frame step, and resolution so the
input/output relationship is inspectable after the PNG is opened.

Use this loop for every inspection or edit:

1. **Resolve and pin scope.** Explicit project and timeline arguments always win.
   For the timeline discovery/read/visualize/render commands, omitted scope is
   resolved by the connected Runtime: the workspace's current project, then
   that project's `metadata.default_timeline_id`. If either selection is
   missing, stop with the recovery command instead of guessing from names or
   ordering. Capture one current authoring head, candidate digest, or explicit
   historical render identity and label the lifecycle (`current`, `candidate
   preview`, `historical`, or `unverified legacy`) in the answer.
2. **Start broad, then narrow.** Get the compact structural overview first.
   Expand an occurrence, then its clip/track/property, then exact text or
   source media. Use the visual view to confirm what is on screen at the same
   target/time; use the text view to explain ownership, roles, bytes, and
   diagnostics. Do not scan the entire media library when a target can be
   addressed directly.
3. **Carry one target between views.** Every result is navigated by the same
   `occurrence_id` plus optional clip, track, media, text/property path and
   half-open time window. Shot names and ordinals are display conveniences, not
   identity. Switching text ↔ visual preserves scope, target, time window,
   expansion, and filters. A missing frame or source is reported explicitly;
   never substitute a nearby frame or a different occurrence.
4. **Choose the smallest supported edit.** Use a public command for a direct,
   exact Runtime operation such as replacing admitted media or rendering. Use
   the [helper catalog](references/document-checkout.md#editing-helper-catalog)
   for sequence, reorder, quantize, ripple, layout, or repeated transforms;
   those helpers operate on the same detached authoring bundle. That code is a
   candidate transform, not a new format or hidden command language. Do not
   edit Runtime files directly, call undocumented endpoints, or silently rebase
   a stale candidate.
5. **Validate and save.** Validate the complete candidate, inspect its diff,
   and save through the existing compare-and-swap/idempotent publication boundary.
   Preview, rendering, and focused readback are optional unless the user asks
   for them. For an explicit preview-before-save request, inspect/deliver a
   successful unpublished candidate render before saving that same candidate.
   Keep the receipt; further corrective edits and saves are allowed.
6. **Explain the evidence.** Report what was observed, what was changed, and
   what remains unavailable separately. A successful command, a semantic
   readback, a decoded media check, and a browser/render proof are different
   claims; do not collapse them into one “done”.

The short agent preamble is:

> Open one pinned composition. Find the target in the structural view, carry its
> exact identity to the visual view, and expand only as needed. Use the smallest
> supported command; otherwise edit a detached same-schema candidate with
> ordinary code. Validate, diff, and save. Choose source inspection, rendered
> preview, or readback when the request needs it. Keep current, candidate, and
> historical evidence distinct.

For discovery, `timelines show` answers structural/text questions, and
`timelines visualize --mode inputs` answers declared-placement questions. They
share the explicit project/timeline, pinned head, and exact
`occurrence_id`; carry that identity between calls rather than using a shot
name, ordinal, or fixture alias. Input mode is not a render or source-pixel
proof. Composed frames come from the exact saved canonical revision; a matching
MP4 is never their source. The operation never publishes a full render as a
side effect.

Workers must treat live Runtime responses and the artifacts returned by those
calls as authoritative. Fixture/evaluator JSON, seed maps, baseline exports,
and prior result files are coordinator inputs or test evidence, never the
source of truth for an agent's conclusion. If a live operation or artifact is
unavailable, say so and stop the unsupported claim.

This contract deliberately favors simple primitives for common work and code
for complex batch work. It does not add a DSL, persistent checkout, new media
store, or automatic merge service.

## Extension data and programmatic passes

Use the shared schema's open maps when a timeline needs application data. Keep
the scope explicit:

| Scope | Extension point | Good use |
| --- | --- | --- |
| Whole timeline | `candidate["parent"]["config"]["app"]["structure"]` | delivery-pass version or project-wide structure |
| One shot/revision | `candidate["shots"][shot_id]["payload"]["metadata"]` | delivery instructions, review status, or generation notes |
| One occurrence | `candidate["placements"][i]["provenance"]` | occurrence-specific processing state or receipt |
| One internal clip/effect | clip `app` or `params` | labels, effect controls, or local analysis |

`config.app.structure` is the intentional timeline extension point; an
unregistered top-level `config.structure` is invalid. These maps are retained
by the detached authoring bundle and published through the same validation,
diff, preview, and compare-and-swap boundary. They are storage extensions, so
the browser/editor needs an explicit projection before it can display or edit
an arbitrary key. Use a namespaced object such as `metadata["delivery"]` for
agent-owned state. If a shot revision is placed more than once, keep different
per-occurrence state in that placement's `provenance` rather than overwriting
shared shot metadata.

For a repeatable pass, use canonical `timelines show`/`open_composition` to pin
the current head and enumerate occurrence identities, then open the same
coordinator-issued target as a detached bundle. The target's
`head_revision_id` must equal the inspected `summary.head_revision_id`:

```python
shown = client.timelines.open_composition(project_id, timeline_ref, detail=True)
assert shown.ok
inspection = shown.data
assert target["head_revision_id"] == inspection["summary"]["head_revision_id"]

bound = client.open_authoring_target(target)  # same project/timeline/head
work = bound.open()                          # exact immutable closure
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
diff = bound.diff(work)       # inspect requested paths before saving
frozen = bound.preview(work)  # candidate JSON; no pixels are rendered
receipt = bound.publish(work, idempotency_key=frozen["candidate_digest"])
```

This is one candidate and one publication, so the status ledger and timeline
change share one head. On a stale-head/CAS error, discard the detached
candidate, run `timelines show` again, and rerun the deterministic pass. Do not
merge stale JSON or publish one shot at a time. For a report-only pass, walk
the ordered occurrences in `inspection`; for an item-level report or edit, use
the same `work["shots"][...]["internal_timeline"]` closure:

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

# For duration changes, use retime/quantize helpers with an explicit policy;
# then repeat validate -> diff -> preview -> publish on this same candidate.
bound.validate(work)
```

Stable Runtime identities remain immutable; compilation and CAS publication
allocate or reuse revisions as appropriate. The full extension-field example
and report/edit guide are in
[`docs/timeline-editing-guide.md`](../../../../docs/timeline-editing-guide.md).

### Renderer and skill boundary

For custom Remotion effect creation, file edits or Inspector controls, use [project-owned element authoring](references/generated-elements-authoring.md). Effects execute only from admitted Astrid packs. Inspect the real source files, preserve the same placement, validate/admit, then wait for the canonical expected-head save receipt. Historical embedded source is migration material, never a fallback execution path.

Remotion and timeline effects stay on this route: edit the admitted internal
timeline `effects`/clip `app` or `params`, freeze the candidate, and use
`render_authoring_candidate_preview` when composed pixels are required. Do not
create a renderer-specific timeline format or send effect edits through the
production orchestrators in `video_editing`. The source pack directory and
capability IDs intentionally remain `rendering` for runtime compatibility;
the agent-facing skill name is `timeline_editing`. After changing this skill,
refresh the writable views with `python3 -m astrid.skills sync --all` and verify
with `python3 -m astrid.skills sync --all --check --json` so the root gateway and
installed pack view do not route existing-timeline work back to video editing.

## Discover and inspect

Resolve the project explicitly whenever more than one project is visible. When
the workspace already has the intended project selected, the `--project` flag
and timeline ref can be omitted; the Runtime uses its current project and the
project's `metadata.default_timeline_id`. List timelines for a compact
inventory, then show the selected timeline for its identity, current head, and
version. Canonical head-only reads may omit the
legacy `config` and `registry` fields; use the SDK composition opener below
for the pinned structural view:

```bash
python3 -m astrid projects list --json
python3 -m astrid projects current --json
python3 -m astrid timelines list --project <project> --json
python3 -m astrid timelines show --project <project> <slug-or-id> --json
python3 -m astrid timelines history --project <project> <slug-or-id> --json
python3 -m astrid timelines diff --project <project> <slug-or-id> --json
```

For SDK callers that need the same bounded structural result as the CLI,
`client.timelines.open_composition(project, ref, ...)` is the shared read-only
adapter. It returns the pinned query, summary, targets, rows, pagination,
media classifications, diagnostics, and scope-preserving actions used by
`timelines show`; it does not create a second timeline document. The adapter's
scope marks source-media actions as metadata-only until Runtime exposes a
digest-bound source handle, so agents must not claim source playback or exact
frame access from `client.media.show` alone:

```python
opened = client.timelines.open_composition(
    None, None, occurrence="<occurrence-id>",
    range_value="10..15", limit=20, detail=True,
)
```

Passing `None` for either scope uses the same Runtime selection rules as the
CLI; pass an explicit project or timeline ref whenever you need a different
scope.

Keep the returned `head`, parent/revision identity, candidate digest, and
render identity together. A cursor is valid only for that complete scope;
reopen the composition after a head change instead of continuing an old page.

When the user asks for visual continuity review, use the filmstrip. It composes the requested
frames from the exact saved canonical revision, including when a matching
successful MP4 exists. `--render-run` can select the revision to inspect, but
the MP4 never supplies frame pixels. The result is a PNG contact sheet,
Markdown, and machine-readable frame index. Omit `--out`; Astrid owns the run
and returns local delivery paths for verified copies of the published evidence
objects. The durable authority is the managed run's digest-verified
bundle/manifest, not those disposable local paths.

Input placement inspection is also available without a render. Run
`timelines visualize --mode inputs --format md --format png` to
project the current canonical input snapshot into declared visual lanes; the
Runtime publishes the Markdown/PNG view as derived project objects. This
reads no rendered pixels, decodes no audio, and makes no provider call.
Placement metadata establishes
which sources and intervals are declared, not what those sources look like in
the rendered composition. The native input PNG is a metadata diagram, not a
thumbnail sheet: it shows separate numbered clip spans, boundaries, a time
ruler, and an occurrence/track/clip legend. It displays at most 40 clips;
narrow the selectors or inspect the paired Markdown/JSON for the rest.

To inspect an image source itself, take `source_object_id` from the exact
selected clip in the returned inspection and retrieve it through the SDK:

```python
source_bytes = client.media.read_bytes(clip["source_object_id"])
```

Open those bytes in an image viewer (or save a temporary inspection copy and
use the agent's image-viewing tool). This verifies the source image, not its
crop, effects, or appearance in the composition. Use the installed Astrid
Python environment for these commands (`python3 -m astrid`) so declared
dependencies are available.

To compare composed pixels, use `--render-run <exact-run-id>` to select the
saved revision pinned by that successful run, or `--revision-id <revision-id>`
to select a saved revision directly. Frames are composed from that exact
revision either way; the run's MP4 is never their source. If both selectors
are supplied, they must identify the same revision. Retain the selected
revision with any conclusion.

When `output` and `inputs` are both shown, the primary page uses a paired-row
layout: five (or the requested `--columns`) output samples per row with the
input lanes relevant to that row immediately underneath. The row's linear
half-open time axis is shared by cards, placements, audio rails, and the PNG
and `static_surface.rows` JSON contract.
Paired pages show one row by default (five columns unless changed with
`--columns`; `--columns 6` gives six across), while standalone output/input
pages keep their normal page sizing. `--page-size` changes the paired layout
only and does not make
standalone pages use the same card count. Pass `--page-size N` explicitly to
opt into a denser paired page, capped at two rows / 10 cards. Multi-page paired
results are intentional: open the numbered pages in order, then rerun with
`--range START..END --every 0.25` or `--shot first` / `--shot N` to drill down.
The frame index records the effective page size, page count, row ranges, and
copyable navigation guidance.

When the user asks for a complete review pass, use `--show output,inputs,text,audio --every 5`.
The command reports the primary PNG, all numbered pages, and a bounded inspect
command. Open the pages in order; then use `--range 0..25 --every 0.25 --detail`
to zoom into one window, `--shot first` (or `--shot N`) to focus an authored
shot, and `--track vo` to isolate the voice lane. Use `--every` for seconds,
`--every-frames` for exact frame steps, `--columns`/`--page-size` for layout,
and `--resolution` for thumbnail size. The generated navigation metadata
contains copyable versions of these commands, so the full-timeline result is
the starting point rather than a dead-end overview.

```bash
python3 -m astrid timelines visualize <slug-or-id> --project <project> \
  --view filmstrip --render-run <exact-render-run-id> --every 0.5 --columns 5 --page-size 10 --include-media --json
python3 -m astrid timelines visualize <slug-or-id> --project <project> \
  --view filmstrip --render-run <exact-render-run-id> --at 12 --context 3 --every-frames 6
```

Use `--sample interval` (default) for regular time samples, `--sample clips`
for picture clips, `--sample cuts` for cut boundaries, and `--sample shots`
for authored story beat midpoints. Shots require authored shot metadata.
An explicit `--every` or `--every-frames` interval is a strict periodic grid;
pass `--include-cuts` when cut-neighbor frames should be added explicitly.
Shot-script context is retained in JSON/details and is shown once per shot
occurrence on review cards when no word/phrase timing exists; later samples
stay visually quiet while their machine-readable status is “same shot; no new
timed text.” Frozen timed speech captions
still take precedence, and “No timed text available” is used only when there
is no applicable timed phrase or shot script.
Use `--range 10..20`, `--shot`, `--clip`, or `--asset` to restrict the view.
The unified inspector can search dialogue, filter shots and time ranges, reduce
density, expand declared visual/audio tracks on one shared absolute-time ruler,
and enlarge a frame or copy its time, revision-scoped target, and pinned focus
command. When the admitted render has audio, the disclosure adds bounded
multi-resolution waveform bins, measured “Quiet gap” intervals, and only
explicitly admitted immutable speech annotations. Audio rows are frozen to the
render; they never turn missing script into silence or present the rendered mix
as an isolated stem. Selecting an audio interval seeks the optional bundled
media and keeps the same frame/clip/track target model. If a clip has no
captured frame in its interval, the inspector says so and exposes the exact
focus command.
Density controls only select already captured frames; rerun with a finer
interval for additional detail. Extraction is bounded at 2,000 frames, so use
a coarser interval or a narrower range for long renders.

The PNG waveform uses a bounded display-only gain/contrast curve so quiet voice
remains visible; the JSON retains the measured amplitudes. Source audio-track
rails are timing signifiers (not per-source loudness measurements), and use the
same absolute time axis as the cards and input placements. In input lanes, each
rail is centered vertically inside its source clip and its label gets a dark
backing chip for legibility.

`auto` and `composed` capture the bounded requested frames through the
canonical Remotion compositor against the exact saved revision, even when a
matching successful MP4 exists. The explicit render-free `--mode inputs` route
does not produce composed pixels. `--render-run` selects the saved revision for
capture; it never selects video frames. Filmstrips do not substitute source
asset thumbnails. PNG cards show spoken text in quotes. Script captions
remain authored segment text, not word-aligned transcription. Missing or
uncertain speech timing is reported as unavailable/partial, while waveform
inspection remains usable; opening the inspector never starts a provider call.
`--include-media` separately adds a relative, hash-verified MP4 for offline
playback; the attachment does not supply frame pixels. Analysis sidecars are
bounded, keyed by immutable render and settings identity, and verified in the
bundle manifest. A selected saved revision keeps old visual evidence associated
with its own timeline state even after later edits.

For composed-output inspection, the paired filmstrip/storyboard is the
canonical visual surface. It is not the only way to inspect a timeline: the
render-free `--mode inputs` route above exposes declared placements and source
media without presenting them as rendered pixels. There is no separate
structural diagram or frozen-object navigation route. Use `--render-run` to
select the saved revision for capture; `--include-media` can attach its admitted
MP4 separately. Never supply a caller-owned `--rendered-video` path. The frame
index's revision-scoped range/shot/clip/track targets are the canonical
navigation surface.

Use a coarse `--every 5` pass to locate a transition, then rerun the exact
time window with a finer `--every 0.25` (or `--every-frames 1` for frame-level
inspection) and add `--detail`. `--range START..END` is half-open, so the end
sample is excluded. `--shot first` and numeric `--shot N` use authored shot
order; use the exact shot name or a time range when chronological order is
what matters.

### Structural-to-visual navigation recipe

For an agent-facing investigation, keep the structural and visual calls next
to one another and reuse the returned target:

```bash
# 1. structural overview/current head
python3 -m astrid timelines show --project <project> <timeline> --json

# 2. visual overview from the revision pinned by the exact successful run
python3 -m astrid timelines visualize <timeline> --project <project> \
  --view filmstrip --render-run <exact-render-run-id> \
  --show output,inputs,text,audio --every 5 --json

# 3. narrow both views to the same shot/time/clip/asset/track
python3 -m astrid timelines visualize <timeline> --project <project> \
  --render-run <exact-render-run-id> --shot <shot-id> \
  --range <start>..<end> --clip <clip-id> --detail --json
python3 -m astrid timelines inspect --manifest <manifest-path> \
  --section cards --limit 20 --json
```

The exact structural expansion is supplied by the authoring-bundle SDK when
the CLI `show` payload is not enough; it must return the same occurrence IDs,
media digests, and intervals that the visualizer uses. A result that cannot be
mapped back to the pinned scope is incomplete, not a new identity.

## Edit and publish

The timeline-evaluation worker reads this checked-in skill at
`astrid/packs/rendering/docs/SKILL.md` when its case brief supplies that path.
The skill file is guidance, not an installed Python package: reading it does
not install Astrid. When the runtime package is installed, the public SDK
import below is the supported API; a checkout-only environment must report
that import/runtime capability as unavailable rather than inventing it.

### Canonical edit bundle

Use the same pinned parent composition for direct edits and code-driven batch
edits. `timelines show` opens a read-only inspection projection; it is not the
editable bundle. For an exact parent-media replacement, use the supported
`timelines replace-parent-media` command only when the case-specific target
receipt declares that route and supplies its required locator. For other
supported edits, use ordinary Python against the detached same-schema bundle:

```python
from astrid.sdk.authoring_bundle import (
    open_authoring_bundle, validate_authoring_candidate,
    diff_authoring_candidate, preview_authoring_candidate,
    publish_authoring_candidate,
)

candidate = open_authoring_bundle(
    pinned_parent, shot_revisions=pinned_shots,
    internal_timeline_revisions=pinned_internal_timelines,
)
# Make the requested small edit or run a deterministic batch transform here.
validate_authoring_candidate(candidate)
diff = diff_authoring_candidate(candidate)
frozen = preview_authoring_candidate(candidate)  # freezes candidate JSON; no pixels are rendered
publication = publish_authoring_candidate(candidate, writer, idempotency_key=run_id)
# Retain the receipt; reopen its exact closure only if a later check needs it.
```

Keep the exact publication response, including its `new_head` and complete
`dependency_manifest`. If readback is needed, reopen that returned closure
rather than a seed map or an agent-authored snapshot. These operations are
public where documented; an unsupported case-specific route must be reported
as unavailable, not inferred from the composition's shape.

`preview_authoring_candidate` and `render_authoring_candidate_preview` are
deliberately different operations. The former freezes a deterministic JSON
candidate for validation/diff/publication; it does not render pixels or prove
that a backend can play the result. The latter consumes that frozen candidate,
renders pixels through the runtime, and returns a render receipt/evidence
identity. Use the public `astrid.sdk.authoring_bundle` import shown above; do
not reach through `astrid.core` as a substitute public API.

For an explicitly requested composed preview, import
`render_authoring_candidate_preview` from that public module and render the
frozen candidate. Check its success and inspect/deliver the output before
publication when the user asks for that order (as in A01). If the required
preview fails, do not save. See the
[target-bound editing guide](../../../../docs/timeline-editing-guide.md) for
the optional preview branch and current source/artifact access limitations.

Keep authoring-bundle `placements` distinct from parent `occurrences`.
Placements are candidate rows describing where an item is placed in the
detached edit; parent occurrences are committed closure identities used for
readback and navigation. A placement may carry an occurrence reference, but
it is not a new parent occurrence and must not be substituted for one.
Start timing normally uses `row["placement"]["start_ms"]`; an existing
`row["at_ms"]` takes precedence, so preserve the opened convention. Duration
uses **`row["duration_ms"]` at the row level**, not inside `placement`.

Do not reconstruct or save a legacy whole-document `config`/`registry` payload
from `timelines show`. Use the detached checkout recipe above so the complete
parent/shot/internal closure, managed media identities, validation diff, and
compare-and-swap publication stay together. For reusable project shots, use
the nested `timelines shots` product only where its command is explicitly
available.

The current public CLI does not expose a group command under `timelines shots`
or a route that groups existing timeline clips into a named shot. Use the supported
project-level shot operations to create or inspect a reusable shot and add
exact same-project media items:

```bash
python3 -m astrid timelines shots create --project <project> --name "Opening" --json
python3 -m astrid timelines shots add <shot-id> --project <project> \
  --media <picture-media-id> --json
```

Grouping existing timeline clips remains an unimplemented product route; do
not infer it from the reusable-shot commands above.

The supported commands above create, inspect, populate, remove, and reorder
project-level shot records. They do not rewrite a timeline document or replace
multiple authored clips with a shot. Use the detached checkout recipe for that
complete composition edit, with Runtime head comparison at publication.

Keep narration in the shot's canonical `voiceover_script` text binding alongside
its voiceover media. Do not leave the only copy in a generation script:

```bash
python3 -m astrid timelines shots text set <shot-id> --project <project> \
  --kind voiceover_script --text-file <script.txt> --expected-head 0
python3 -m astrid timelines shots text list --project <project> \
  --kind voiceover_script
```

For timeline-wide draft VO updates, the owner is
`video_editing.sync_draft_voiceover`; this timeline skill defines the structure
and timing rules it preserves. The workflow reads script text pinned to the
selected composition revision. Missing or invalid script bindings are reported,
and an intentionally empty script is skipped. It only replaces VO that it owns
as draft; existing final, recorded, or locked audio is protected. Adopting a
legacy clip requires its exact clip ID in `adopt_clip_ids`.

The sync can keep every shot window with `timing_policy="preserve"`; narration
that exceeds a window then fails before publication. With
`timing_policy="ripple"`, duration changes move later shots and parent visual
layers. Ripple rejects overlapping shot placements, source offsets or non-unit
speed, parent or internal audio that would need stretching, and parameterized
layers without an explicit `app.draft_voiceover_timing="stretch"` declaration.
Inspect its detached plan and diff before deciding whether to apply it. It does
not render a preview or change the live timeline while preparing speech; a
successful apply validates the candidate and saves one revision through the
authoring bundle's head check.

Publication accepts registered text descriptors in `shot["payload"]["text_bindings"]`.
The descriptor's `binding_id`, `head`, `media_id`, `content_hash`, and byte size pin
one verified revision; publication checks the registered head for a changed shot
revision. Arbitrary embedded-only narration and conflicting metadata copies are
rejected. Keep the registration receipt and publish that exact descriptor. For a
new reusable shot, publish its media/structure first to register it, register its
narration with head `0`, then publish the descriptor through the parent CAS. These
are explicit supported steps; registration alone does not change an existing
composition's pinned narration. A stale head requires a fresh checkout and explicit
reconciliation, never substitution of the latest text.

The runnable checkout example includes this complete path for a registered shot:

```bash
python3 scripts/timeline_document.py checkout --project <project> --timeline <timeline> --file /tmp/edit.json
python3 scripts/timeline_document.py bind-script --file /tmp/edit.json --shot <registered-shot-id> \
  --text-file <script.txt> --expected-head 0 --idempotency-key narration-01
python3 scripts/timeline_document.py check --file /tmp/edit.json
python3 scripts/timeline_document.py publish --file /tmp/edit.json --idempotency-key pin-narration-01
python3 -m astrid timelines script <timeline> --project <project> --json
# Historical read: use the publication receipt's exact parent revision.
python3 -m astrid timelines script <timeline> --project <project> --revision <parent-revision> --json
```

`client.timelines.script(project, timeline, revision_id=None, occurrence=None)`
is the same public reader. It returns names, occurrence identities, declared
track/timing, verified text and binding provenance, excludes unplaced shots,
and marks missing narration separately from deliberately empty text. It never
silently reads the latest binding for a pinned composition. Single-click and
open in the timeline editor address the placed occurrence, including a shot
whose internal timeline has no clips. Temporal overlap does not establish child
ownership; occurrence placement controls the shot's lane and window.

`list` and `show <binding-id>` include the verified text. Use head `0` to create
a binding; read its current head before updating it. The binding belongs to
the registered shot referenced by the timeline; it does not add visible text
or regenerate audio. Managed renders pin its immutable text identity and head
in provenance. When importing an existing script, verify that it corresponds
to the shot's current voiceover audio.

The config must be renderable before spending a render attempt. Keep clip types
explicit, use registered element IDs for custom visual elements, and keep the
output/profile compatible with the authoritative theme canvas. Read
[references/timeline-cookbook.md](references/timeline-cookbook.md) when
constructing or checking the JSON shape.

For the constrained transparent PNG layer supported by the FFmpeg backend, use
one ordinary managed `clipType: "media"` clip with a positive `hold` on a
visual track placed first, followed by exactly one ordinary visual picture
track:

```json
{
  "tracks": [
    {"id": "overlay", "kind": "visual"},
    {"id": "picture", "kind": "visual"}
  ],
  "clips": [
    {"id": "overlay", "at": 0, "track": "overlay", "clipType": "media", "asset": "managed-alpha.png", "hold": 1.0},
    {"id": "picture", "at": 0, "track": "picture", "clipType": "media", "asset": "managed-video.mp4", "from": 0, "to": 1.0, "volume": 0}
  ]
}
```

The registry entry for `managed-alpha.png` must resolve to a local, probed PNG
whose decoded dimensions exactly match the visual canvas and whose PNG bytes
declare transparency. This subset supports one full-duration static layer;
arbitrary transforms, crop, per-layer blending, and multiple held overlays are
unsupported. The overlay is looped, bounded to its hold interval, normalized to
the canvas, and composited after the base visual concat; existing text overlays
still use their current path. Stream copy is disabled for this overlay path.

## Render and open

Only when the user asks for a render, an exported file, or a rendered preview.
Otherwise publish and give the editor link (see the section above). A previous
render does not by itself authorize re-rendering after subsequent edits.

When a preview render is needed (including reference-frame and storyboard
previews), use `--review` by default. Deliver and open that review render so the user can identify frames
by shot name and timecode. Use meaningful registered shot names, and check that
the labels are visible in the exported video. A filmstrip viewer is useful
alongside the video, but does not replace its review overlay. Omit `--review`
when the user requests a clean or final export.

Render through the product command. The positional reference is a runtime slug,
UUID, or ULID; it is never a file path. Rendering pins the current kernel
snapshot. Add `--expected-version` when the observed version must remain
unchanged, select a qualified backend only when needed, and use `--detach` only
when admission without terminal completion is intended:

```bash
python3 -m astrid timelines render <slug-or-id> --project <project> \
  --expected-version <version> --review --output-name <name>-review.mp4 --json
```

Review mode shows the registered shot name and running timeline time in the top-right corner in Remotion and Three.js, plus the pinned authored voiceover script as a readable bottom caption. Caption timing is the canonical shot interval and is explicitly marked non-word-aligned; no ASR timing is invented. Names and captions are pinned from canonical shot references and text bindings before expansion. Gaps show `No shot`; overlapping shots show all active names. The overlay exists only in this render; saved timeline documents are unchanged. FFmpeg rejects review mode explicitly; choose a review-capable backend for editorial previews rather than silently dropping the flag. SDK inputs use `"review": true`.

Remotion review renders default to a backend-native low resolution that fits the
authored canvas inside 640x360 while preserving its aspect ratio. The canonical
canvas remains in the timeline props and all positions are evaluated there;
Remotion's `--scale` performs the output scaling. The resulting artifact
profile records the dimensions Remotion actually emits. Such renders carry the
exact `Low Res Render` label at top left, while shot name and timecode remain at
top right. A clean render, or a render with an explicit profile, keeps the
existing full-resolution behavior.

Review labels and subtitles are inspection overlays. Keep the authored
creative composition centered in the full canvas; do not shift primary visual
content or reserve a caption-safe band for review text unless the user
explicitly requests a caption-safe design. Review typography scales with the
authored canvas under `--scale`, while badge readability is handled separately.

The default waits for completion and propagates terminal failure. A successful
render records its run and provenance in the runtime. Review the newest
successful render, or an exact run, through the runs surface. Opening video is
currently supported by the native macOS opener; Linux/AgentBox playback does
not imply that `open` or `afplay` exists. On Linux, use the returned verified
local media path with an installed player or decode/inspect it headlessly, and
report interactive playback as unavailable when no player/display is present:

```bash
python3 -m astrid runs open --project <project> --timeline <slug-or-id>
python3 -m astrid runs open <run-id> --project <project>
```

Use `--default-timeline` for the project's default. If no matching successful
render exists, inspect `runs list/show`, and render only if the user asked
for one.

The [render capability contract](../actions/render/STAGE.md) covers detailed
SDK inputs and outputs; the [visualization contract](../actions/timeline_visualize/STAGE.md)
covers evidence navigation.

## SDK equivalent

Use the typed client or `astrid.sdk` when embedded in a program. Canonical
render uses `timeline_ref`, not a path-backed `timeline`:

```python
import astrid.sdk as sdk
result = sdk.invoke(
    "rendering.render", project="<project>",
    kind="action",
    inputs={"timeline_ref": "<slug-or-id>", "expected_version": 4},
)
```

Use `client.timelines.visualize` for declared-input or composed-output evidence
and `client.timelines.show` / `client.timelines.open_composition` for bounded
text inspection. Composed visualization captures from the exact saved
composition without requiring a video render; use `rendering.render` when an
MP4 output is needed. Programmatic edits use the detached authoring-bundle
validate/publish path above; mutable whole-document timeline saves are retired.
Keep `project` explicit and
use returned runtime IDs, manifests, and receipts for durable navigation.

## Renderer authoring

When building or extending a renderer, read the
[pack-builder skill](../../_core/docs/pack-builder/SKILL.md) and the protocol
contract at [docs/contracts/render-backend-v1.md](../../../../docs/contracts/render-backend-v1.md).
Renderer packs advertise qualified protocol capabilities; timeline editing and
the public `rendering.render` facade remain runtime-owned. Do not add a new
facade, direct module invocation, or backend-specific shape to the timeline.

## Runtime-owned multi-step stitching

For a two-child generation flow that must wait durably and preserve declared
output order, use the typed handoff in
`astrid.packs.video_editing.shared.runtime_orchestration` and the stitch
admission in `astrid.packs.rendering.shared.runtime_stitch`. These modules
describe the graph and publication settings; the Runtime owns lifecycle,
continuation admission, canonical timeline CAS, and render-task creation. The
registered `rendering.assemble_timeline` executor consumes the claimed
`resolved_children` envelope and emits a deterministic authoring proposal. The
generic pack host sends that proposal through the fenced
`publish_timeline_render` checkpoint, which creates the ordinary
`rendering.render` task. Claim that task through the same host to run the
existing renderer and retrieve its output objects and receipt. Do not add a
pack-local timeline save, scheduler, polling loop, or second renderer.
