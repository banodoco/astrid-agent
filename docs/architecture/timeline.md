# Timeline architecture

**Status:** Agent-owned as-built page for PLAN M4/D11; M2 remains active. The
M4 foundation is not complete: this page does not claim milestone or gate
completion, a
delivery ZIP, or zero fallback writers. Historical R4 control documents remain
archived. See the [editing guide](../timeline-editing-guide.md) and
[authoring capabilities](../timeline-authoring-capabilities.md) for usage.

Timeline has one accepted authored model and three surfaces. **Read** is a
revision-pinned projection: Agent owns timeline meaning and text/CLI views,
Runtime owns the accepted closure and history, and App owns presentation.
`timelines show`, the Python inspection client, and the sheet export are read
projections of that model; the covered native sheet adapter emits commands when
editing is supported, while detached-sheet publication remains legacy. **See**
asks the published Composition evaluator what is on screen; preview,
visualization and export stay outside save. **Edit** has one accepted contract
for migrated operations: every producer builds a registered `EditCommand`, the
Agent provider supplies its meaning, and Runtime alone accepts and records it.
SDK/Python, CLI, App gestures, and covered sheet edits are adapters to that
path; they are not alternate writers.

The Agent command registry and semantic provider record writes (including
unchanged writes), reads, selections/order, bindings and created IDs. Runtime's
ordinary `/v1` commit door loads that provider before readiness, validates the
declared inputs and authority, and rebases non-conflicting stale commands on
the server. Runtime stores the only accepted history and closure. Each client
outbox seals the exact in-flight request and retry identity while retaining
newer local commands; a retry reuses that flight rather than recapturing it.
Undo and restore are new guarded commits; neither rewinds history nor invokes
the renderer.

Supported derived timing is compiled by
`astrid/sdk/timeline/compiler.py::compile_timing` and
`_timing_evaluators.py`. Saves validate structure, timing, references and
authority without a renderer. Managed Scene is an explicit, advisory
diagnostic boundary: `scene_only.py` asks the pinned host for Scene answers and
keeps unknown or unavailable explicit. It is not save authority.

Producers store immutable artifacts and submit settled proposals through the
same command path; a proposal or dry run is not a publication receipt.
Catalog/reference metadata is separate from the accepted film closure.

The public Agent integration described here is `00e73f925`. Its current tuple
is published014/Main497 with Runtime's published95 source/producer lock
`95bd3c5f`; the ordinary command contract has 55 definitions and 29
operations (`6ba7b51e`). Composition C13 is `0.0.13`, the 77-file archive at
SHA-256
`9412ae25c306d5118241f4d631e8eecc6eef9b2fb5a98302d40938e9f6d54738`.
These are current source/contract facts, not an M4 completion claim.

Still-live migration boundaries are explicit: WC1's digest-only media
publication remains legacy; WC7's bounded attached move/outbox path does not
retire detached or broader publication; WC8's covered connected sheet cells
are native while detached sheet merge/legacy grammar remains; WC10's ordinary
`timeline.init` v1 is merged in the current registry, while Agent
`RemoteTimelines.create_empty` still uses the old publication path and only
its custom raw-config v2 source/migration is staged; App activation status is
separately owned and is not inferred from WC10; and D13's assembled-media
admission/proposal is staged while the legacy host publication remains.
Checkout/whole-bundle writers, remaining timing and App
snapshot/parent/popup/checkpoint writers stay held.

Private017 remains qualification-only and unqualified: its bounded direct
service evidence does not establish public HTTP, native-command, or marker
conversion behavior. The future transition work likewise remains staged: the
ordinary transition source has not passed its selected-reference-presence
review, so its combined future carrier is not a public command, pin, or
activation. Neither status is evidence that the current legacy callers have
been migrated.

Staged branches, private installed profiles, and future carrier proposals are
qualification material only: they are not merged product behavior, pins,
public APIs, or evidence that fallbacks are gone.

**Code anchors:** Agent `astrid/sdk/timeline/{commands,commit,outbox,sync,compiler,producer}.py`;
Runtime `runtime_protocol/{daemon,timeline_commit_provider,timeline_commit_port,timeline_commit_store}.py`;
App `src/integrations/runtime/dataProvider.ts` and
`src/tools/video-editor/commands/commitOutbox.ts`; Composition
`typescript/src/{evaluation,scene-evaluation}.ts`.
