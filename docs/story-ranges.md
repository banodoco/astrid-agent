# Story ranges and the `story_overview` producer

The authored table is `parent.config.app.astrid.ranges`. Each row has exactly
`{name, kind, from, to}`; `kind` is `chapter` or `beat`. TimeRefs are strings in
`astrid.core.timeline.moments`' existing grammar. `to: null` means film end.
Ranges are half-open `[from, to)`; chapters cannot overlap, and beats may overlap.
There are no persistent row ids or cached range times. New writes/migration use
the existing integer `app.astrid.v = 2` compatibility declaration, alongside the
current authored-music format; no separate `requires` or version carrier is added.

`astrid.sdk.timeline.story.migrate_story_ranges(bundle)` migrates a detached
working copy and returns findings. `Checkout` also performs this migration on
load, after the existing lossless namespace migration. The migration does not
resolve/rewrite clip timing, touch the Runtime, save, or create an edit/journal.
It validates each cut's live picture anchor and checks its current frame against
the canonical picture-target resolver. A cut TimeRef (`c1`) remains a cut TimeRef.
Copying the picture's `on` text to an ordinary word TimeRef would change the
first-word exception: a picture opens at the take origin; the word starts later.
The shared cut projection and resolver retain that distinction through reflow,
trim and speed under the exact compiler's existing transcript coordinate policy.
Swapping an asset leaves the range fixed; moving the narration anchor moves the
corresponding chapter with its cut.

Labels are ordered by the shared cut index, equal consecutive chapter names are
coalesced, and each end becomes the next chapter's start. Missing, unanchored,
orphaned, stale or ambiguous cut anchors keep the original legacy `chapters`
table plus findings. The entire label table is converted atomically: partially
removing labels would lose the following boundary on a later retry. Repeating
migration changes nothing. Existing canonical chapters, unsupported label rows,
unknown writer versions, or opaque namespace collisions are preserved with
findings. Existing canonical beats are preserved when legacy labels migrate.
Foreign Runtime `app` fields and collision archives retain the namespace rules.
`intent.chapters()` is now explicitly the legacy accessor; `intent.ranges()`
returns detached canonical data. The existing `set_chapters` compatibility writer
replaces chapter data while preserving beats. No new authoring command is added.

`set_ranges(bundle, rows)` is a storage accessor: it validates on a defensive
compiler copy, refuses unsupported/unresolved anchors, invalid rows and chapter
overlaps, then writes only the namespace. `resolve_ranges(checkout)` is the
shared projection used by chapter selectors, sheet headings and checks. Its
input is an already compiled checkout. Selectors refuse invalid ranges; checks
report invalid canonical data as blocking and retained legacy labels as findings.

The extension-facing module is **`astrid.sdk.timeline.story`**:

```python
from astrid.sdk.timeline.story import open_story_overview

view = open_story_overview(
    project_id, timeline_id, revision_id=exact_revision_id, client=host_client,
)
```

This host boundary fetches the exact requested closure with the existing
`client.timelines.open_bundle`, verifies project/timeline and pinned revision,
and calls `story_overview(bundle, revision_id=...)`. It never defaults to a moving
head. The direct function is for an immutable bundle supplied by the host;
`base_parent` alone does not authenticate a locally edited draft. The SHA-256
`content_digest` also binds the result to the exact serialized input. No new
service, registry, view protocol, or extension execution path is introduced.

The plain JSON read model is:

```python
{
    "project_id": "p", "timeline_id": "t", "revision_id": "rev-0",
    "content_digest": "<SHA-256 of canonical serialized input>", "fps": 30.0,
    "ranges": [
        {"range": {"name": "01 OPEN", "kind": "chapter", "from": "c1", "to": "c2"},
         "start_frame": 0, "end_frame": 120},
    ],
    "legacy_chapters": [],
    "findings": [],
}
```

Each `range` is the authored canonical row, and each resolved edge is an integer
frame or `null`. Findings have `{code, path, message}`, with positional paths
such as `ranges[0].from`. Unsupported TimeRefs and unresolved picture anchors
are reported; cached seconds are not substituted for unresolved values. On a
compiler failure all returned canonical edges are `null`. Overlapping or
collapsed intervals remain visible with findings. Consumers must inspect
findings before treating a row as a valid story interval.

Resolution calls **`compile_timing(TimingInputs(bundle), commands=())`** on the
compiler's defensive copy, then the existing moments parser/resolver/frame policy
through `_MomentContext`, whose cut boundaries use the F `CutIndex`. Compiler
notes are reported separately from ordinary successful retime change summaries.
Neither the caller's bundle, a live Checkout, its journal nor the pinned Runtime
revision is modified. Returned rows are detached from the input and cut cache.

A minimal real example using the disposable tiny fixture:

```python
import json
from pathlib import Path
from astrid.sdk.timeline.story import set_ranges, story_overview

bundle = json.loads(Path("tests/fixtures/timeline_editing/tiny.json").read_text())
set_ranges(bundle, [
    {"name": "01 OPEN", "kind": "chapter", "from": "c1", "to": "c2"},
    {"name": "02 LIVE", "kind": "chapter", "from": "c2", "to": None},
    {"name": "viral", "kind": "beat", "from": '"viral"', "to": 'after "viral"'},
])
view = story_overview(bundle, revision_id="rev-0")
assert [(r["start_frame"], r["end_frame"]) for r in view["ranges"]] == [
    (0, 120), (120, 240), (39, 51),
]
assert view["findings"] == []
```

These are explicitly authored disposable example ranges. Tiny's pictures have no
`on` declarations: converting the old selector example's `chapters` labels keeps
them legacy and emits findings instead. The trimmed reflow fixture provides the
positive anchored-migration examples. Neither fixture is authenticated historical
film evidence. A#golden-baseline, golden/v8/v9/v11 start-frame comparisons and the
v8 eight-chapter golden comparison remain pending; no STORY acceptance is claimed.
