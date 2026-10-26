# CLI journeys — runtime-backed product commands

This guide walks the five product families (`projects`, `media`, `tasks`,
`runs`, `timelines`), the two nested mounts (`media references`, `timelines
shots`), and the runtime health/backup routes. Examples use `--json` where the
command returns an SDK envelope. The Astrid Runtime is the sole
authority for all durable state; the Astrid checkout has no live project store.

Normative references: `docs/astrid-first-sprint-plan-20260813.md` (Sprints 5–6),
`docs/contracts/platform-contract.md` (envelope contract).

---

## Runtime preamble

Configure the installed local workspace runtime before issuing product
commands. The first product command starts or reconnects it through the
explicit launcher boundary:

```bash
export ASTRID_RUNTIME_SOURCE_MANIFEST=/path/to/astrid-source-profile.json
python3 -m astrid projects list --json
```

The SDK's explicit `AstridClient.open(...)` accepts a loopback endpoint and
credential directly; it does not discover or infer either value. Run commands
from any directory; project, object, receipt, and event state remains in the
runtime. `astrid doctor` is read-only and does not trigger first-run bootstrap.

```bash
# 1. Confirm the CLI is reachable (prints the product census).
python3 -m astrid --help

# 2. Run the read-only first-run diagnostic (state=uninitialized is expected
#    before the first project exists).
python3 -m astrid doctor --json

# 3. Inspect a concrete family and verb without side effects.
python3 -m astrid projects --help
python3 -m astrid timelines show --help
```

Notes:

- **Neutral runtime handoff for product commands.** Product families below
  cross the generated workspace client, and the configured neutral launcher
  starts or reconnects it on first use.
- **One verb = one SDK call.** Every handler parses arguments, makes exactly
  one SDK service call, and renders the result. There is no SQL or domain
  logic in the CLI layer.
- **`--json` is the stable product machine surface.** Product and nested-mount
  commands print exactly one five-key envelope (see below) to `stdout`; the
  read-only `doctor --json` diagnostic has its documented check/state shape;
  human operational commands print concise summaries instead.

---

## The five-key envelope

`--json` always emits exactly one JSON object with these five keys:

| Key               | Meaning                                                            |
| ----------------- | ------------------------------------------------------------------ |
| `ok`              | `true` on success, `false` on a typed SDK error                    |
| `data`            | the command's result payload (`null` on failure)                   |
| `error`           | a frozen error object (`{code, message, details}`) or `null`       |
| `receipt`         | the committed command receipt on mutations, `null` on reads/failure|
| `idempotency_key` | caller-supplied key, or the key the SDK generated before mutation  |

Success shape:

```json
{
  "ok": true,
  "data": {"id": "…", "slug": "demo"},
  "error": null,
  "receipt": {
    "receipt_id": "…",
    "command_kind": "…",
    "idempotency_key": "…",
    "request_hash": "…",
    "project_id": "…",
    "project_seq": [1, 1],
    "event_ids": ["…"],
    "result": {"…": "…"},
    "created_at": "…"
  },
  "idempotency_key": "…"
}
```

Failure shape:

```json
{
  "ok": false,
  "data": null,
  "error": {"code": "validation_error", "message": "…", "details": {"…": "…"}},
  "receipt": null,
  "idempotency_key": "…"
}
```

Exit codes are stable: `0` success (`ok=true`), `1` typed SDK error
(`ok=false`), `2` usage/parse error (argparse).

---

## 1. `projects` — create / list / show / update / select / current

```bash
# create — one client.projects.create call (slug immutable, idempotency key returned)
python3 -m astrid projects create demo --name "Demo" --json

# list — one client.projects.list call (slug ascending)
python3 -m astrid projects list --json

# show — one client.projects.show call by id or slug
python3 -m astrid projects show demo --json

# update — one client.projects.update call (name and/or settings delta)
python3 -m astrid projects update demo --name "Demo Renamed" --json

# select — persist a workspace/user project-routing preference
python3 -m astrid projects select demo --scope workspace --json

# current — inspect the selected project and supplying preference scope
python3 -m astrid projects current --json
```

Pass `--project` explicitly to ordinary project-scoped commands. `runs open`
can instead consume the runtime `select/current` preference;
`ASTRID_PROJECTS_ROOT` routing is historical and is not Stage1 authority.

---

## 2. `media` — import / list / show / verify / relate

```bash
# import — accepts ONLY a file or directory; one exact-media result per file
python3 -m astrid media import ./shot.png --project demo --json
python3 -m astrid media import ./assets --project demo --json

Video/audio containers are strictly checked with `ffprobe` before admission.
An undecodable `.mp4`/`.wav` (including a Git-LFS pointer) returns a typed
`validation_error` with no media row, event, receipt, or managed bytes; install
the ffmpeg package when `ffprobe` is unavailable and retry. Generic files and
images retain their extension-based import classification.

# list — project-scoped (created_at, then id)
python3 -m astrid media list --project demo --json

# show — exact project-scoped media id
python3 -m astrid media show M_01ABC --project demo --json

# verify — fingerprint-verified; requires --realm (all matching locations)
python3 -m astrid media verify M_01ABC --project demo --realm managed_local --json
# verify one runtime object precisely
python3 -m astrid media verify M_01ABC --project demo --realm managed_local --json

# relate — one typed relation edge; frozen five-kind --kind
python3 -m astrid media relate --project demo \
  --from M_01ABC --to M_02DEF --kind derived_from --json
```

Media relation `--kind` is restricted to the frozen five kinds:
`derived_from`, `variant_of`, `uses_as_input`, `mask_for`, `audio_for`.

---

## 3. `media references` — create / update / archive / associate / link / set-primary / list / show

The `references` family is a manifest-declared **nested mount**: it is
reachable only beneath `media` (for example, `astrid media references list`) and is
never a top-level command.

```bash
# create — one client.references.create call; frozen --kind, --name, --media
python3 -m astrid media references create --project demo \
  --kind character --name "Aria" --media M_01ABC --json

# update — name/description/metadata delta (kind and project stay immutable)
python3 -m astrid media references update R_01ABC --project demo \
  --name "Aria (S1)" --json

# archive — soft terminal mutation; every byte and association is preserved
python3 -m astrid media references archive R_01ABC --project demo --json

# associate — one exact media row with a frozen --role
python3 -m astrid media references associate R_01ABC --project demo \
  --media M_02DEF --role depicts --json

# link — one typed reference link; related_to is symmetric
python3 -m astrid media references link --project demo \
  --from R_01ABC --to R_02DEF --kind belongs_to --json

# set-primary — atomic primary-canonical replacement by association id
python3 -m astrid media references set-primary R_01ABC --project demo \
  --media-reference MR_01ABC --json

# list — active references by default; --include-archived is the explicit read
python3 -m astrid media references list --project demo --json
python3 -m astrid media references list --project demo --include-archived --json

# show — always includes archived references
python3 -m astrid media references show R_01ABC --project demo --json
```

Frozen vocabularies:

- `--kind` (create): `character`, `place`, `object`, `clothing`, `other`
- `--role` (associate): `canonical`, `used_as_input`, `depicts`, `inspired_by`
- `--kind` (link): `belongs_to`, `wears`, `located_in`, `associated_with`, `related_to`

These are the same tuples the repository enforces against the DDL `CHECK`
constraints — see `tests/v10/test_vocabulary_verification.py` for the
drift-detection proof.

---

## 4. `tasks` — create / list / show / cancel / retry / events

```bash
# create — admit one immutable task (spec is a JSON object)
python3 -m astrid tasks create --project demo \
  --capability gen.upscale --spec '{"size": 2}' --json

# list — project-scoped (created_at, then id)
python3 -m astrid tasks list --project demo --json

# show — one task's full immutable read model
python3 -m astrid tasks show --project demo T_01ABC --json

# cancel — one nonterminal task (no executor fence is exposed)
python3 -m astrid tasks cancel --project demo T_01ABC --json

# retry — one eligible failed/expired task
python3 -m astrid tasks retry --project demo T_01ABC --json

# events — the task's ordered core.task stream events
python3 -m astrid tasks events --project demo T_01ABC --json
```

`tasks retry` retries a single task. Batch retry over a run group is the
`runs retry` surface (next section), not a `tasks` flag.

### VibeComfy: import → inspect → edit → validate → run

VibeComfy has three entry paths:

```bash
# Standalone local work: no Astrid history.
vibecomfy import ./workflow.json
vibecomfy edit workflows/workflow set sampler.steps 24
vibecomfy validate workflows/workflow

# Standalone CLI with explicit Astrid tracking.
vibecomfy import ./workflow.json --project demo
vibecomfy edit workflows/workflow --project demo set sampler.steps 24
```

From an Astrid project, use the native task flow below. It inherits Astrid's
project/task context, so its executor does not receive VibeComfy's
`--project` flag and does not create a nested task.

The canonical bundle has three sibling members: editable `workflow.py`, its
identity/layout companion `workflow.vibe.json`, and byte-identical original
`source.json`. The companion is required beside Python. `inspect` remains
read-only: its projection and inspection report help explain a workflow but
are not the workflow authority or valid edit input. `edit` writes a canonical
Python/companion successor and a transition report; an atomic batch records
its ordered operations as one accepted revision. Direct Python or applied
canvas changes enter history only through explicit `capture`. Validation
checks the edited artifact; it does not run generation.

First store the raw workflow with `media import`. This stores the source as a
project-managed object; the following `vibecomfy.import` task records its
canonical origin. In the media-import response, `object_id` is the
content-address (`sha256:<hex>`) and equals the SHA-256 `digest`. Put the
returned digest in `spec.input_digests` for the named `source` port and the
returned `object_id` in `--input-manifest` to authorize the managed object.
These values have the same spelling, but serve different roles. In SDK code,
pass the returned object ID in `input_manifest=[...]`:

```bash
python3 -m astrid media import ./workflow.json --project demo --json

python3 -m astrid tasks create --project demo \
  --capability vibecomfy.import \
  --spec '{"inputs":{"workflow_id":"portrait"},"input_digests":[{"name":"source","digest":"sha256:<SOURCE_DIGEST>"}],"transition_kind":"origin","parent_task_id":null,"origin_task_id":null}' \
  --input-manifest '["sha256:<SOURCE_OBJECT_ID>"]' --json
```

The origin task emits `workflow.py`, `workflow.vibe.json`, unchanged
`source.json`, and `edit-report.json`. The report records the origin identity,
revision, readiness, and exact member digests. Settlement registers each
output with the project's managed object store before task completion, so a
named output digest is also its content-addressed `object_id` and can be
admitted directly by the next task. Do not download and re-import these
outputs. Each accepted edit or capture names its parent and origin task IDs.
Read the output digests from `result.outputs` in the origin task response;
each entry has a port `name` and `digest`:

```bash
python3 -m astrid tasks show --project demo <ORIGIN_TASK_ID> --json
```

`vibecomfy.inspect` can then report on the canonical bundle without creating a
revision. Loading a canonical Python bundle executes its generated Python, so
canonical inspection requires the explicit scalar
`python_execution_consent="confirmed"`. This value has no default and is not
implied by task admission. UI JSON inspection stays static and needs no consent.
Supply all three canonical member digests as named inputs and put those same
content-addressed IDs in the input manifest:

```bash
python3 -m astrid tasks create --project demo \
  --capability vibecomfy.inspect \
  --spec '{"inputs":{"python_execution_consent":"confirmed"},"input_digests":[{"name":"python","digest":"sha256:<PYTHON_DIGEST>"},{"name":"companion","digest":"sha256:<COMPANION_DIGEST>"},{"name":"source","digest":"sha256:<SOURCE_DIGEST>"}]}' \
  --input-manifest '["sha256:<PYTHON_OBJECT_ID>","sha256:<COMPANION_OBJECT_ID>","sha256:<SOURCE_OBJECT_ID>"]' --json
```

`vibecomfy.edit` accepts the same canonical trio plus explicit
`python_execution_consent="confirmed"`, and a typed operation or ordered
batch. The input is required, has no default, and must exactly match the value
shown; an admitted task alone is not consent. This same consent applies to
`manual_capture`, including direct Python candidates. Edit emits the complete
successor trio with a transition report containing the input and VibeComfy's
audited gate decisions.
Put the ordered typed operations in a JSON file and import it as a managed
object. The edit task names the exact parent revision/task and the origin task.
Import `operations.json` with `media import`; use its `digest` in the named
`operations` port and its `object_id` in the input manifest. Each spec digest
binds a named port; the content-addressed `sha256:<hex>` value is also the
managed object ID that must be authorized by the input manifest:

```json
{
  "schema_version": 1,
  "expected_revision": 0,
  "ops": [
    {"op": "edit_node", "target": "ksampler", "field": "steps", "value": 24}
  ]
}
```

```bash
python3 -m astrid media import ./operations.json --project demo --json

python3 -m astrid tasks create --project demo \
  --capability vibecomfy.edit \
  --spec '{"inputs":{"workflow_id":"portrait","parent_revision":"<PARENT_REVISION>","parent_task_id":"<ORIGIN_OR_PREVIOUS_EDIT_TASK_ID>","origin_task_id":"<ORIGIN_TASK_ID>","transition_kind":"typed_edit","python_execution_consent":"confirmed"},"input_digests":[{"name":"python","digest":"sha256:<PYTHON_DIGEST>"},{"name":"companion","digest":"sha256:<COMPANION_DIGEST>"},{"name":"source","digest":"sha256:<SOURCE_DIGEST>"},{"name":"operations","digest":"sha256:<OPERATIONS_DIGEST>"}],"workflow_id":"portrait","parent_revision":"<PARENT_REVISION>","transition_kind":"typed_edit","parent_task_id":"<ORIGIN_OR_PREVIOUS_EDIT_TASK_ID>","origin_task_id":"<ORIGIN_TASK_ID>"}' \
  --input-manifest '["sha256:<PYTHON_OBJECT_ID>","sha256:<COMPANION_OBJECT_ID>","sha256:<SOURCE_OBJECT_ID>","sha256:<OPERATIONS_OBJECT_ID>"]' --json
python3 -m astrid tasks show --project demo <EDIT_TASK_ID> --json
```

The edit task emits the complete successor trio and `edit-report.json`. Its
settled outputs are managed objects in the project too; copy the new named
output digests from `tasks show` into both the next task's named input digests
and input manifest. In `result.outputs`, match each entry by port `name`; its
`digest` is the object ID. Use the successor trio for validation:

For a direct Python change, call `vibecomfy.edit` with the same parent trio,
the same exact consent input, set `transition_kind` to `manual_capture`, and
provide the candidate as the separate `capture_python` input. An applied
ComfyUI canvas can be captured through `capture_graph`. Neither route creates
invented per-tool operations; the report records the aggregate before/after
difference and audited consent gate decisions.

```bash
python3 -m astrid tasks create --project demo \
  --capability vibecomfy.validate \
  --spec '{"inputs":{"python_execution_consent":"confirmed"},"input_digests":[{"name":"python","digest":"sha256:<EDITED_PYTHON_DIGEST>"},{"name":"companion","digest":"sha256:<EDITED_COMPANION_DIGEST>"},{"name":"source","digest":"sha256:<SOURCE_DIGEST>"}]}' \
  --input-manifest '["sha256:<EDITED_PYTHON_OBJECT_ID>","sha256:<EDITED_COMPANION_OBJECT_ID>","sha256:<SOURCE_OBJECT_ID>"]' --json
```

After validation succeeds, pass that same immutable canonical trio to
`vibecomfy.run` only when you want to execute the workflow:

```bash
python3 -m astrid tasks create --project demo \
  --capability vibecomfy.run \
  --spec '{"inputs":{},"input_digests":[{"name":"python","digest":"sha256:<EDITED_PYTHON_DIGEST>"},{"name":"companion","digest":"sha256:<EDITED_COMPANION_DIGEST>"},{"name":"source","digest":"sha256:<SOURCE_DIGEST>"}]}' \
  --input-manifest '["sha256:<EDITED_PYTHON_OBJECT_ID>","sha256:<EDITED_COMPANION_OBJECT_ID>","sha256:<SOURCE_OBJECT_ID>"]' --json
```

A successful canonical validation emits `validation-report.json` with the
explicit consent input and gate audit; static UI JSON validation omits consent.
A run can launch ComfyUI generation; validation by itself does not. The
inspect task's projection is an explanation artifact only. Never pass it as
the workflow input to edit, validate, or run. A manual capture records one
aggregate before/after change and does not invent individual tool operations.

Discover and read the existing Astrid task history with the normal `tasks`
commands:

```bash
python3 -m astrid tasks list --project demo --json
python3 -m astrid tasks show --project demo <ORIGIN_OR_EDIT_TASK_ID> --json
python3 -m astrid tasks events --project demo <ORIGIN_OR_EDIT_TASK_ID> --json
python3 -m astrid tasks follow <ORIGIN_OR_EDIT_TASK_ID> --project demo
```

`task.admitted` and terminal task events come from Astrid's shared events
table. The completed task's output digests are also the project's managed
content-addressed object IDs for its immutable report and bundle members;
semantic operations and before/after revisions live in the report, not in a
custom event stream. `tasks list` discovers the chain;
`tasks show` and `tasks events` inspect a specific transition, while `follow`
observes its lifecycle. The task spec/report parent and origin IDs connect
successive transitions. See the [VibeComfy pack skill](../../astrid/packs/vibecomfy/skill/SKILL.md)
for the capability input/output contract and editing details. The input
manifest is the runtime authorization fence: each `spec.input_digests` value
binds a named port, and the matching `sha256:<hex>` object ID must appear in
`--input-manifest`.

---

## 5. `runs` — list / show / cancel / retry / events / open

```bash
# list — project-scoped (started_at, then id)
python3 -m astrid runs list --project demo --json

# show — run read model with derived child progress (optional --evidence)
python3 -m astrid runs show --project demo RUN_01ABC --json
python3 -m astrid runs show --project demo RUN_01ABC --evidence --json

# With --evidence, successful child completion outputs are also returned:
# media ids, roles, labels, hashes, sizes, and safe relative paths for the
# render and provenance artifacts.

# cancel — drive every queued, blocked, or running child to terminal cancelled
python3 -m astrid runs cancel --project demo RUN_01ABC --json

# retry — batch retry (see semantics below)
python3 -m astrid runs retry --project demo RUN_01ABC --json
python3 -m astrid runs retry --project demo RUN_01ABC \
  --task T_01ABC --task T_02DEF --json

# events — the run's ordered core.run stream events
python3 -m astrid runs events --project demo RUN_01ABC --json

# open — latest successful render in the selected current project
python3 -m astrid runs open

# open one exact render; --project overrides the selected current project
python3 -m astrid runs open RUN_01ABC
python3 -m astrid runs open --project demo

# open the latest successful render belonging to the main/default timeline
python3 -m astrid runs open --project demo --default-timeline

# choose a canonical timeline explicitly
python3 -m astrid runs open --project demo --timeline primary
```

`runs open` uses runtime run/task records and managed object bytes only. It
never scans checkout files or sorts filenames by modification time. "Latest"
means the newest successfully settled `rendering.render` run; Astrid does not
yet expose a separate editor-approved/current-deliverable promotion pointer.
`--default-timeline` resolves the project's `metadata.default_timeline_id`;
`--timeline` selects a canonical timeline by slug or id. Both require matching
runtime run/task provenance and report an error if no matching successful
render exists. A filename alone does not establish a timeline match.
The stored video is verified against its runtime SHA-256 and size, then opened
on macOS through a named hard link. Opening does not download or duplicate the
video.

### Batch retry semantics (frozen)

`runs retry` has exactly two modes, and the decision is frozen:

- **All-failed-children (default).** With no `--task` flag, the command
  retries every eligible failed/expired child of the run
  (`selected_task_ids=None`).
- **Explicit subset.** Repeatable `--task <id>` restricts the retry to an
  exact ordinal subset (`selected_task_ids=[T_01ABC, T_02DEF, …]`).

There is no `--run` flag on `tasks retry`; the batch retry surface is
`runs retry`, and this is the frozen policy.

---

## 6. `timelines` — create / list / show / script / replace-parent-media / archive / recover / history / diff / visualize / render

```bash
# create — one client.timelines.create call (slug immutable)
python3 -m astrid timelines create --project demo --slug primary --json

# list — compact identities and counts for active timelines (slug ascending)
python3 -m astrid timelines list --project demo --json

# show — full timeline config and asset registry, by UUID, ULID, or slug
python3 -m astrid timelines show --project demo primary --json

# replace-parent-media — narrow canonical parent-composition CAS replacement;
# the exact current head must be supplied and stale heads fail closed.
python3 -m astrid timelines replace-parent-media primary --project demo \
  --occurrence-id OCC_01ABC --clip-id CLIP_01ABC \
  --source-object-id MEDIA_SHA256 --expected-head REV_01ABC --json

# Complete-document edits use the detached checkout recipe. Edit the JSON
# between checkout and check. Check returns its report; publish rewrites the
# file with managed media digests before one parent compare-and-swap, then
# returns the receipt, media_imports, and edited document.
python3 -m astrid timelines document checkout \
  --project demo --timeline primary --file /tmp/timeline-edit.json
python3 -m astrid timelines document check \
  --file /tmp/timeline-edit.json --json
python3 -m astrid timelines document publish \
  --file /tmp/timeline-edit.json --idempotency-key EDIT_01ABC --json

# archive — event-backed terminal mutation
python3 -m astrid timelines archive --project demo primary --json

# history — ordered lifecycle events (read)
python3 -m astrid timelines history --project demo primary --json

# diff — deterministic adjacent-version diffs (read)
python3 -m astrid timelines diff --project demo primary --json

# render — version-pinned canonical render; waits for completion by default
python3 -m astrid timelines render primary --project demo \
  --expected-version 1 --backend rendering.remotion \
  --output-name primary.mp4 --json

# visualize the delivered render — the paired rendered filmstrip is the only
# timeline visualization; --include-media enables playback.
python3 -m astrid timelines visualize primary --project demo \
  --render-run latest --include-media --json

# queue only — explicit admission semantics, returned state is "admitted"
python3 -m astrid timelines render primary --project demo --detach --json
```

---

## 7. `timelines shots` — list / create / show / add / remove / reorder

The `shots` family is a manifest-declared **nested mount**: it is reachable
only beneath `timelines` (for example, `astrid timelines shots list`) and is never a
top-level command.

```bash
# list — every shot in a project (sort_key, then id)
python3 -m astrid timelines shots list --project demo --json

# show — inspect one shot's ordered item/media mapping from a fresh read
python3 -m astrid timelines shots show S_01ABC --project demo --json

# create — one empty shot
python3 -m astrid timelines shots create --project demo --name "Shot 1" --json

# add — insert one exact same-project media item at a validated position
python3 -m astrid timelines shots add S_01ABC --project demo \
  --media M_01ABC --position 0 --json

# remove — remove one item (its kernel media row and bytes are preserved)
python3 -m astrid timelines shots remove S_01ABC I_01ABC --project demo --json

# reorder — one whole-shot permutation; omissions/duplicates are rejected
python3 -m astrid timelines shots reorder S_01ABC --project demo \
  --items I_02DEF,I_01ABC --json
```

`reorder` accepts a repeatable/comma-separated `--items` list naming the
entire item-id permutation. A permutation that omits, duplicates, or adds an
item id is rejected by the service before any write.

Shots are project-level reusable records. The `timelines shots` nesting is a
CLI mount for discoverability, not an implicit timeline association, and shot
commands therefore do not take a timeline id. If a timeline document chooses
to reference a shot, that relationship lives in the document's own config;
removing a shot id from a document does not delete the reusable shot record.
`show` returns ordered item ids, media ids, positions, and best-effort media
name/path details so a fresh agent can inspect state without retaining a
mutation response.

---

## Exit codes

| Code | Meaning                                        |
| ---- | ---------------------------------------------- |
| `0`  | success (envelope `ok=true`)                   |
| `1`  | typed SDK error (envelope `ok=false`)          |
| `2`  | usage/parse error (argparse `SystemExit(2)`)   |

Scripts should parse the `--json` envelope for outcome details and treat the
process exit code as the coarse success/failure signal.

## 8. Diagnostics and failure recovery

Run the runtime health check before changing a project:

```bash
python3 -m astrid doctor --json
```

If it reports `state: "unavailable"`, run the Astrid install line again and
retry. Do not create a local database,
tail local event files, or edit runtime state by hand. An unexpected command
failure is returned as a typed error; preserve the idempotency key and retry
only when the error's recovery guidance permits it.

Canonical timeline replacement and detached publication are compare-and-swap
operations. A stale expected head is an HTTP `409 timeline_version_conflict`
(or SDK `stale_version`) and changes nothing. For a narrow media replacement,
load the current timeline and use the exact returned revision. For a complete
document edit, check out a fresh candidate, merge locally, run `check`, and
publish only if the Runtime head still matches:

```bash
python3 -m astrid timelines show --project demo primary --json
python3 -m astrid timelines replace-parent-media primary --project demo \
  --occurrence-id OCC_01ABC --clip-id CLIP_01ABC \
  --source-object-id MEDIA_SHA256 --expected-head REV_01ABC --json
```

For missing or byte-mutated media, run runtime verification again. The service
rejects the read without rewriting the object; the public envelope reports the
typed `integrity_error` code for this failure.

```bash
python3 -m astrid media verify M_01ABC --project demo \
  --realm managed_local --json
```

When a runtime route is unavailable, the command reports the typed
`unavailable` condition and its next action. Backup operations are runtime-owned
gateway routes; use `backup create`, `backup restore`, `backup export`, or the
realm lifecycle operations with `--json` when automation needs the result.
