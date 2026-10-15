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
Only the trusted local SDK host is supported. App, CLI, sheet, remote execution,
untrusted Python, pinned-clip policy and publication are unsupported in this example.
Beat grids are attached using existing `clip.set-beats`; no analysis runs here.
