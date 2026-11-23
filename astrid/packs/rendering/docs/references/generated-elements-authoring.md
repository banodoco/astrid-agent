# Project-owned Remotion effects

Effects have one source and execution path: an Astrid V3 rendering element pack with ordinary files. A timeline holds an exact owner-qualified reference, settings and timing. Do not create JSX in assistant messages or timeline `app.elements`. Historical embedded code is recovery/migration material, not an executable fallback.

## One place for effects

Author every new or changed effect in the project's pack: `project-pack init` → edit files → `validate` → `admit` → one timeline commit for the edit. Do not start a second pack, copy the effect elsewhere, or put code in the timeline.

Promote to the Personal pack only when the user asks for reuse. Ask first; promotion is not automated yet.

Built-in and installed-pack elements are never edited. A new effect is always a fork under a NEW element id (same kind), in the project pack. Elements are keyed by (kind, id) and the first owner wins, so built-ins and installed packs cannot be overridden by an id collision.

No core or vendored code change is ever needed for an effect. If an effect seems to need one, the fix belongs in the pack (its element, helpers or declared resources).

## Inspect before editing

Use the current authenticated project's exact timeline:

```bash
astrid-tools timelines elements --project <project-id> <timeline-id>
```

This returns the saved head, clip/ref identity, current settings/timing, actual canonical component and descriptor paths, schema, source/admission status, and supported commands. A legacy effect is explicitly `legacy-migration-required`; an absent source is not a successful editable pack. Existing project Markdown preferences remain user-owned and verbatim; generated machine paths belong to this inspection/context, not preference prose.

A project draft pack is durable user data outside application checkouts, `.otto`, site-packages and build caches. It is not `/tmp` and has no automatic expiry while timelines depend on it. No pack is created during inspection. When custom code is needed, use the returned explicit lazy command:

```bash
python3 -m astrid.core.element.cli project-pack init --realm <realm-id> --project <project-id>
python3 -m astrid.core.element.cli project-pack inspect --realm <realm-id> --project <project-id>
```

The current root is selected by `ASTRID_PROJECT_PACKS_ROOT` (default `~/.local/share/astrid/project-packs`). The source binding is selected by the project’s Runtime document: kind `astrid.project-pack`, payload schema `astrid.project-pack/v1`, stable default document ID. Its portable binding and admitted refs use normal document version/CAS; local paths and compiler argv stay in native configuration. There is no realm-wide JSON authority or appended preference prose. Additional collections use their stable pack ID as the document name. Do not guess hidden source folders, edit product packages, silently use the shared Personal pack, or hand-edit the generated selection artifact.

## Ordinary files, then validation and admission

Read the Pack Builder skill for the V3 declaration shape. Add one `rendering` entry of `type: element` to the pack's `pack.yaml`. Declare its `element.yaml` and `component.tsx` plus every helper/asset resource. The descriptor owns the JSON Schema and defaults; the TSX component uses ordinary React/Remotion imports and consumes those settings. Supported imports are React, React JSX runtime, Remotion and declared relative resources. No arbitrary dependencies or dynamic source loaders.

Edit these files with normal filesystem tools. For existing effects preserve the element ID, clip ID and current user settings. Only change timing when requested. Keep animation deterministic from frame and seed; test true square geometry and completion at the authored end, including delay/fade/shrink boundaries.

```bash
python3 -m astrid.core.pack.cli validate <pack-root> --json
python3 -m astrid.core.element.cli project-pack admit --realm <realm-id> --project <project-id>
```

Admission validates canonical ownership/resources, import closure and full content revision, verifies a bounded immutable source snapshot, compiles each element with the renderer's own pinned Node and Remotion esbuild, and CAS-selects the project pack document only after the compile succeeds. No toolchain setup is needed on an installed stack. If the renderer Node or Remotion esbuild is missing or mismatched, the admit error names the repair; then admit again.

Derived per-pack indexes are reconstructible caches, not authority. Invalid or changing source fails; the last admitted revision remains available. The snapshot is derived build input; canonical files remain the editable source. Old pinned revisions never silently resolve to new mutable code. No timeline is changed by admission.

After a successful admission, every editor (dev, built and hosted) shows the admitted project-pack elements with no app deploy: the editor loads the compiled module from the Runtime.

## Admit, then make one commit

Use the exact owner-qualified reference returned for the admitted element. Stage placements with the other timeline edits in one `Timeline.commit` function:

```python
from astrid.sdk.timeline.api import Timeline

element = {
    "packId": "<pack-id>",
    "id": "<element-id>",
    "kind": "effect",
    "revision": "<admitted-revision>",
}

tl = Timeline.open("<project-id>", "<timeline-id>")

def edit(t):
    t.clip("<first-clip-id>").set_element(element)
    t.clip("<second-clip-id>").set_element(element)
    t.clip("<first-clip-id>").params["<setting>"] = "<value>"

receipt = tl.commit(edit, label="Place the admitted effect")
```

Put every placement and its related parameter, text, or timing edits in that function. The commit function may be evaluated again after a conflict, so keep external side effects out of it. A successful receipt is the evidence that the edit was saved. Admission and the timeline commit are separate calls: admission makes the code available, and the commit changes the timeline.

## Update the same placement and confirm

Use the returned exact admitted pack/element/revision and the captured timeline head:

```bash
astrid-tools timelines update-element --project <project-id> <timeline-id> \
  --realm <realm-id> --clip-id <existing-clip-id> --pack <pack-id> \
  --element <same-element-id> --kind effect --revision <admitted-revision> \
  --expected-head <captured-head> --duration <explicit-requested-seconds> \
  --params-json '<intended-settings-object>'
```

Omit duration when timing was not requested; omit params when source/schema alone changed. Existing settings are preserved and intended keys merge. The command updates one existing generated hold effect using the canonical authoring bundle and expected-head publication. It preserves start/track, other media, registries, transcript references and pinned child closure. A stale head fails without automatic rebase; inspect the new state before deciding a fresh edit. Do not add a duplicate overlay to replace an existing effect.

For a single existing built-in effect clip (for example `am-discord` or `am-footage`, including inside a shot), the one-target `place-element` command remains available: `astrid-tools timelines place-element --project <project-id> <timeline-id> --realm <realm-id> --target occurrence:<occurrence-id>/clip:<clip-id> --pack <pack-id> --element <forked-element-id> --kind effect --revision <admitted-revision> --expected-head <captured-head>`. It sets the clip type and element ref together and keeps the clip's params, timing and media. Use `Clip.set_element(element)` inside `Timeline.commit` when the placement belongs with other edits in one commit.

Only a successful durable `new_head` receipt establishes a saved edit. A file write, validation, admission, emitted JSON or optimistic prose does not. Report partial failures honestly. After the saved receipt, check that the open editor shows the change without a reload and inspect its controls (no render unless the user asks; composed `visualize` frames are fine). Never resend user queued chat messages for verification.
