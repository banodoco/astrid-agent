---
name: beat-tools
description: Fit a selected run of overlay clips in one cut to existing beat moments.
---

Discover `timeline.operation/beat_tools.fit_to_beats` using
`astrid.sdk.timeline.tools.discover_operations`. Invoke it with `invoke_operation`.
Pass ordered unique `clips` and exactly N+1 `beats` (current beat/downbeat moment
strings). Empty selection with no beats is a no-op. Start and end anchors are
replaced; the timing compiler retains the beat references when music is retimed.
Use `dry_run=True` to see old/new intent and computed changes without durable writes.
Contribution execution uses the trusted local SDK host. App, remote execution,
untrusted Python and pinned-clip policy remain unsupported.
Beat grids are attached using existing `clip.set-beats`; no analysis runs here.

The timeline CLI can also use the trusted local SDK runner via `edit --command`.
Discover the operation from an admitted project-local or managed source first,
then serialize `operation_call(operation, inputs).to_json()`. Its OpCall is
`timeline.operation` with `identity`, `implementation_digest`, and `inputs` in
`args`. A stale implementation digest refuses before editing. The same call may
join `timeline.batch`; the Plan records the expanded guarded `clip.on` and
`clip.until` commands. Publish those recorded commands through the normal draft
publication path. Runtime refuses an unexpanded `timeline.operation` request.
This adapter does not grant App or remote hosts permission to execute Python.
