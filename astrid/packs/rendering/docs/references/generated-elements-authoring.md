# Project-owned Remotion effects

Effects have one source and execution path: an Astrid V3 rendering element pack with ordinary files. A timeline holds an exact owner-qualified reference, settings and timing. Do not create JSX in assistant messages or timeline `app.elements`. Historical embedded code is recovery/migration material, not an executable fallback.

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

The current root is selected by `ASTRID_PROJECT_PACKS_ROOT` (default `~/.local/share/astrid/project-packs`). The source binding is selected by the project’s Runtime document: kind `astrid.project-pack`, payload schema `astrid.project-pack/v1`, stable default document ID. Its portable binding and admitted refs use normal document version/CAS; local paths and compiler argv stay in native configuration. There is no realm-wide JSON authority or appended preference prose. Additional collections use their stable pack ID as the document name. Do not guess hidden source folders, edit product packages, silently use the shared Personal pack, or hand-edit the generated selection artifact. Promote/copy a useful effect to Personal only when the user requests reuse.

## Ordinary files, then validation and admission

Read the Pack Builder skill for the V3 declaration shape. Add one `rendering` entry of `type: element` to the pack's `pack.yaml`. Declare its `element.yaml` and `component.tsx` plus every helper/asset resource. The descriptor owns the JSON Schema and defaults; the TSX component uses ordinary React/Remotion imports and consumes those settings. Supported imports are React, React JSX runtime, Remotion and declared relative resources. No arbitrary dependencies or dynamic source loaders.

Edit these files with normal filesystem tools. For existing effects preserve the element ID, clip ID and current user settings. Only change timing when requested. Keep animation deterministic from frame and seed; test true square geometry and completion at the authored end, including delay/fade/shrink boundaries.

```bash
python3 -m astrid.core.pack.cli validate <pack-root> --json
python3 -m astrid.core.element.cli project-pack admit --realm <realm-id> --project <project-id>
```

Admission validates canonical ownership/resources, import closure and full content revision, verifies a bounded immutable source snapshot, and CAS-selects the project pack document only after its configured compiler accepts it. Derived per-pack indexes are reconstructible caches, not authority. Invalid or changing source fails; the last admitted revision remains available. The snapshot is derived build input; canonical files remain the editable source. Old pinned revisions never silently resolve to new mutable code. No timeline is changed by admission.

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

Only a successful durable `new_head` receipt establishes a saved edit. A file write, validation, admission, emitted JSON or optimistic prose does not. Report partial failures honestly. Refresh the existing editor after the saved receipt, inspect controls in the editor (no render or composed frames unless the user asks), and verify normal reload. Never resend user queued chat messages for verification.
