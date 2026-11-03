# Timeline editing guide

For cross-product ownership and the canonical path, see [Timeline architecture](architecture/timeline.md).
The agent-facing summary is the [timeline editing skill](../astrid/packs/rendering/docs/SKILL.md);
this guide is the longer version with worked examples. The two must agree.

## The model in one paragraph

A timeline is tracks of clips placed in seconds. The connected Runtime keeps
every revision. An edit is submitted as one commit; Runtime accepts or
refuses it as a whole; an accepted commit gets one receipt and one undo.
Nothing ripples unless you call a ripple helper. Media files are imported
into the project catalog by content and stay there after undo.

## One path per task

| Task | Path |
|---|---|
| One small, named change | `timelines edit` (media add, literal move, source trim) |
| Rows of a table | `timelines show --as sheet --native`, edit, `timelines apply` |
| Logic, loops, ripple, mixed changes | `tl.commit(fn)` |
| Reviewed exact changes | `tl.commit(list)` from a preview |

A function is intent: on a clash it is evaluated again on the newer
timeline. A list is a decision: it is applied exactly or refused. Previewing a
function and applying its list is one Python path; do not mix the CLI, the
sheet and Python in one task.

## Opening and reading

```python
from astrid.sdk.timeline.api import CommitConflict, Timeline, TimelineEditError

tl = Timeline.open(project, timeline)          # configured runtime connection
tl.clips(track="V1")                           # a filterable list
tl.clip("intro")                               # one clip, or a refusal listing candidates
tl.clips(asset="ROCKET")                       # by the asset key `show` prints
tl.words(between=(10, 20))                     # spoken words with times
tl.word("Meanwhile", n=1)
tl.duration                                    # where the last picture ends, on any video track
max(c.end for c in tl.clips(track="V1"))       # where V1 ends
```

`timeline` is the slug or id that `timelines list --project <project>` prints;
never guess one from a display title. Pass a `client=` to `Timeline.open` only
when you already hold an authenticated SDK client; never recover credentials
from Runtime storage. Shot occurrence ids for `arrange` are
`[p["occurrence_id"] for p in tl.bundle["placements"]]`. That list comes in
the bundle's stored order, which is not promised to be time or editorial
order; build the order you pass to `arrange` yourself. A placement's start is
`at_ms`, or `placement.start_ms` when `at_ms` is absent (milliseconds), so
time order is:

```python
def start_ms(p):
    return p["at_ms"] if p.get("at_ms") is not None else (p.get("placement") or {}).get("start_ms", 0)
by_time = [p["occurrence_id"] for p in sorted(tl.bundle["placements"], key=start_ms)]
```

`tl.clip(name)` matches a clip id or id prefix, an address, a layer name, an
asset key, an element id or on-screen text. When several clips match, it
refuses and lists them; pass the exact id from `show`.

`timelines history <timeline> --project <project> --json` lists commits newest
first in `data.revisions`. Each row's `message` is the commit label,
`publication_id` is the same id a commit returns as `receipt["receipt_id"]`
(what undo takes) and `created_at` its time. To undo a known edit whose
receipt you lost, find its label there. When `data.complete` is false,
Runtime could list only the head.

## The four owner cases

These are the acceptance cases for this editing surface. Each makes one
authored commit, and one undo restores the prior content and order.

```python
# 1. Twenty media items in time order.
def place_twenty(t):
    assert len(items) == 20
    for n, (path, at, hold) in enumerate(sorted(items, key=lambda x: x[1]), 1):
        t.add(path, at=at, hold=hold, track="V1", id=f"shot-{n:02d}")
r = tl.commit(place_twenty, label="Place twenty files", idempotency_key="place-20")
tl.undo(publications=[r["receipt"]["receipt_id"]])

# 2. Arrange twenty real shot occurrences, back to back.
assert len(order) == len(set(order)) == 20
r = tl.commit(lambda t: t.arrange(order, at=0, gap=0), label="Arrange shots")

# 3. New picture and audio tracks with content.
def picture_and_voice(t):
    t.track("V2", above="V1")
    t.add("picture.mp4", at=0, track="V2", id="pic").trim(0, 4)
    t.add("voice.wav", at=0, track="A2", id="vo")
r = tl.commit(picture_and_voice, label="Picture and voice")

# 4. Several kinds of change together, previewed first.
def recut(t):
    f = t.clip("footage").trim(1, 5).on(2)
    t.clip("cutaway").split(8, id="cutaway-right")
    t.clip("title").hold_for(2).on(f.end + 0.25)
    t.track("titles").rename("Opening")
    t.arrange(order, at=12, gap=0.5)
    t.clip("old-intro").remove()
    t.add_text("Chapter one", at=12, hold=3, track="titles", id="ch1")
preview = tl.commit(recut, label="Recut", dry_run=True)
r = tl.commit(preview["data"]["commands"], label="Recut")
```

Case 1 imports up to twenty files and case 3 imports two; the imports are
listed in a preview and survive undo. Name the `track` for `t.add`: it
creates a missing track once, as an audio or a picture track to match the
verified file; an existing track of the wrong kind refuses.

## Helpers

- **`t.add(path, at=, hold=, track=, id=)`**: image, video or audio, at `at`
  timeline seconds. With `hold=N` a still lasts N seconds and a video or
  audio file plays source seconds 0 to N (read it back);
  without `hold` a video or audio file plays its verified duration.
  `.trim(in, out)` then sets the source window.
- **`t.add_text(text, at=, hold=, track="titles", id=)`**: plain text at `at`
  timeline seconds for `hold` seconds. A missing track is created as a
  picture track; an existing track is used where it is, never moved. It takes
  no position or style arguments. Give it an `id` and adjust it with `t.clip(id)`.
- **`t.track(id, above=, kind=)`**, **`.rename(label)`**: create a track or
  select the existing one (never a second), optionally place it above another,
  and label it. Moving an existing track is not supported. `kind` is `"visual"` (default) or `"audio"`; `t.track("music",
  kind="audio")` makes the audio track a music bed needs.
- **`clip.on(t)`, `clip.trim(in, out)`, `clip.hold_for(s)`, `clip.end`**:
  timing. `trim` is in source seconds and keeps the start. `hold_for` sets a
  literal length on any clip; on video or audio the source window keeps its
  in-point and a length the file cannot fill is refused. A plain number given
  to `on` is always timeline seconds. On a plain timeline it also clears any
  moment string the clip had. On a narrated film, where a clip belongs
  to a cut, it is stored relative to that cut's start, so the clip follows
  its cut when the narration is re-flowed; to follow a word, give a moment
  string.
- **`clip.split(at, id=)`**: cuts at timeline seconds through native split and
  returns the left half, which keeps the original id; the right half gets `id`
  (`t.clip(id)`). Requires registry-backed parent media, an interior cut, an
  explicit source window for moving media, and no timing anchors,
  animation or parent transitions.
- **`clip.swap_asset(path_or_key)`**: plays another file (imported) or
  catalog key in place; the start stays. An image or video keeps its length; an audio clip that played
  its whole file plays the whole replacement file. Swapping a music clip that
  has a beat grid to a file without one may refuse until a grid is attached
  (`clip.set_beats`).
- **`clip.set_track`, `clip.set_look`, `clip.remove`** (`remove` leaves a gap).
- **`t.arrange(ids, at=, gap=)`**: shot occurrences in caller order, from
  `at`, `gap` apart (both timeline seconds). It keeps
  their ids, lengths and source windows, leaves their old slots empty,
  refuses unknown lengths and collisions with occurrences you did not select,
  and never ripples.

## Words, beats and ripple

Words are numbers you can compute with (`w.start`, `w.end`). A moment string
(`'after "Astrid"'`, `'beat 2 after "Astrid"'`, `'"tool" in v20a #2'`) is
stored on the clip and re-resolved when the narration moves; the grammar is
in [the editing front door](../astrid/packs/rendering/docs/references/editing.md#moments).
A word said more than once must name its line and number, as `timelines words
<timeline> <word>` prints them. Beats are reachable through moment strings;
`timelines words <timeline> <word> --beats` prints the beats near one word.
There is no full beat list.

Ripple helpers work on flat timelines inside a function, one commit and one
undo each:

```python
tl.commit(lambda t: t.insert_time(4, 2), label="Open 2 s at 4 s")
tl.commit(lambda t: t.ripple_delete(4, 6), label="Remove 4–6 s")
tl.commit(lambda t: t.close_gap(before=t.word("Meanwhile"), keep=0),
          label="Close gap before Meanwhile")
```

Clips after the point move on every track, text included; picture spanning it
is held longer or shortened; clips wholly inside a removed window are deleted.
Other audio spanning a removed window is split at the edit point. The **music
bed** is any audio clip on the track whose id is `music`, or with a beat grid.
A bed that spans the edit is not cut at the edit point; the `music=` policy
below decides what happens to it instead. To make a bed count, put it on
`music`: `t.add(path, track="music")` creates a missing `music` track as an
audio track, and `t.track("music", kind="audio")` creates one for
`clip.set_track("music")` (`kind` defaults to `"visual"`). On a flat timeline a ripple refuses to move a clip that
carries a moment string, animation or transition. Closing a hole on one track
only is not supported.

Cutting a film to 15 seconds with `t.ripple_delete(15, t.duration)` removes
everything after 15 s on every track, including an end card. To keep the
card, remove only up to it, `t.ripple_delete(15, t.clip("end-card").start)`:
the card then starts at 15 s and the film ends one card later.

`insert_time`, `ripple_delete` and `close_gap` each take `music=`:
`"keep_end"` (default) cuts a spanning bed at a nearby bar, not at the edit
point, with a crossfade so it still ends with the film; `"plays_on"`, as in
`t.ripple_delete(4, 6, music="plays_on")`, leaves a spanning bed where it was
and makes what follows move against its beats. Both modes are supported
choices. Only a bed that begins at or inside the edit is unsettled, in either
mode (including a bed that begins at the window's start under `plays_on`).
Do not promise such a bed stays whole; read the bed's clips back after the
commit and tell the person what changed.
`close_gap` measures the silence from the end of the
previous word. Within one function each call sees the previous one, so times
after a ripple have already moved.

## Preview and apply

`tl.commit(fn, dry_run=True)` returns Runtime's commands
(`["data"]["commands"]`) and a readable diff (`["data"]["diff"]`), with
`receipt` `None`. It may import media and lists those imports. It takes no
key and leaves nothing waiting to be sent. Applying the returned list keeps
its exact references, media identities and the expectations it captured; it
never selects again. Calling the function again re-evaluates it and may
produce something other than what was reviewed. Keep the list until the person
answers; if it is lost, preview again and show the new diff.

## Conflicts with a person

Runtime merges disjoint changes from different authors. When a change
clashes, a function can be evaluated again on the new head, up to three
evaluations total. A fixed list is refused as submitted. A real clash saves a
pending record and raises `CommitConflict`; inspect `err.clash["summary"]` and
recover with `tl.retry(err)` when `err.pending_id` is set. Runtime refusals,
including idempotency mismatches, are saved as pending records too, and
`tl.retry(err)` returns `refused` for them. Inspect the record, fix the edit,
then retry or drop it. Follow `err.fix` for refusal recovery; the exception
text may say to retry directly. An error without `pending_id` has no saved
record to recover.

`tl.retry(err)` re-runs function intent. For reviewed lists, it returns a
`preview` if the list still applies, or `re_derive` if it still clashes
(disjoint list edits already merge). Commit only a `preview`; for
`re_derive`, run the function that created the list again on a fresh
`Timeline.open`, preview the result, then commit `data.commands` with
`idempotency_key=` and `pending=<saved id>`. The saved record closes when the
re-derived edit lands. In the CLI, use `timelines pending list/show` and
`timelines retry`; a preview prints the valid `--commit TOKEN` command.

For a same-actor writer sharing a local Runtime session, `session base changed`
and `outbox changed during evaluation` also save a pending record. It contains
the exact commands captured for that evaluation and their original base, and
the SDK raises `CommitConflict` with `pending_id`. Call `tl.retry(err)`, or
resume by that id after a restart. Inspect the saved record and the returned
`preview` or `re_derive` result before committing. Only after inspecting the
saved intent should you deliberately re-derive and preview new commands. An
error without `pending_id` has no saved record to recover.

`timelines discard` clears queued working-copy commands but keeps saved pending
records. It is not a required step before another edit. `CommitConflict` is a
subclass of `TimelineEditError`, so an `except` for it must come first.

Function commits are guarded broadly: any concurrent commit can count as a
clash. Precise read tracking is a planned follow-on, so expect reruns while
someone is actively editing.

## Keys, retries, status and undo

`idempotency_key=` is optional and explicit; nothing derives one for you. A
keyed function call whose key already landed returns the original receipt
without evaluating the function or importing anything, even if the function
is different, so name the task and timeline in every key. After a lost reply,
call again with the same key, or simply call again in the same session: the
saved request is replayed byte for byte. A final refusal releases the
saved request and any queued edits, so the next edit is not blocked.

`timelines status` shows the head and any request with an unknown outcome;
`timelines discard` drops unsent work but keeps a request that may have
landed (it cannot undo one whose reply was lost).

```python
tl.undo(publications=[receipt_id], idempotency_key="undo-42")
tl.redo(publications=[undo_receipt_id], idempotency_key="redo-42")
```

```sh
python3 -m astrid timelines undo TL --project PROJECT --commit RECEIPT_ID --idempotency-key undo-42
python3 -m astrid timelines redo TL --project PROJECT --commit UNDO_RECEIPT_ID --idempotency-key redo-42
```

Undo restores the fields and order the commit changed, so unrelated later
edits survive. It refuses when a later commit overlaps; undo that one first.
Repeating an undo without its key can be refused by the same guard. A refused
commit made no revision, so "undo my last edit" means the last receipt that
landed. Whole revision restore is a deliberate rollback, not undo.

## Series and variants

Run one function per timeline, with one key and one receipt each:

```python
import json

def swap_sponsor(t):
    t.clip("sponsor-card").swap_asset("sponsor-2026.png")

def record(ep, **row):
    with open("sponsor-ledger.jsonl", "a") as f:
        f.write(json.dumps({"timeline": ep, **row}) + "\n")

for ep in episodes:
    try:
        tl = Timeline.open(project, ep)
        if not any(c.id == "sponsor-card" for c in tl.clips()):
            record(ep, status="skipped")             # every outcome gets a row
            continue
        r = tl.commit(swap_sponsor, label="Swap sponsor", idempotency_key=f"sponsor-{ep}")
        record(ep, status=r["data"]["status"], receipt=r["receipt"]["receipt_id"])
    except CommitConflict as exc:                    # before the general case
        if not exc.pending_id:
            record(ep, status="refused", detail=str(exc))
            continue
        try:
            r = tl.retry(exc)
            if r["status"] == "preview":
                record(ep, status="needs_approval", detail=r.get("summary"),
                       pending_id=r.get("pending_id"))
            elif r["status"] == "re_derive":
                record(ep, status="re_derive", detail=r.get("summary"),
                       pending_id=r.get("pending_id"))
            else:
                record(ep, status=r["status"], detail=r.get("summary"),
                       pending_id=r.get("pending_id"))
        except Exception as retry_exc:                # recovery failure is recorded per timeline
            record(ep, status="retry_error", detail=str(retry_exc),
                   pending_id=exc.pending_id)
    except Exception as exc:                         # refused, or no verified reply
        record(ep, status="error", detail=str(exc))
```

Each outcome is written as it happens, so a crash loses nothing. Continue
past a clash and report failures individually. A refusal result keeps its
pending id; inspect the record, fix the edit, then retry or drop it. If an
error has no `pending_id`, record it and continue without retrying. Catch
recovery errors inside the per timeline handler so they cannot stop the
series. Repeat the loop only for landed or idempotent outcomes; failed,
refused, preview, and re-derive outcomes need their recorded follow-up first.
For "the end of V1", compute
it from V1's clips; `t.duration` also counts V2 and titles. There is no
transaction across timelines. A preview list belongs to one timeline: apply
the reviewed literal list to the approved episode rather than rerunning its
function. Run the function for the other episodes (or preview each if each
needs approval).

`tl.duplicate(as_="variant-01")` creates the destination timeline, then
copies the head into it with fresh shot and occurrence ids while keeping the
source's clip ids, so the same names find the same clips (runtime behavior
read it back). It returns
`destination_setup` and `copy_receipt` separately. The source is only read.
Its operation key comes from the project, source and name, so a retry never
creates a second destination. `timelines archive variant-01 --project
<project>` removes an unwanted copy and `timelines recover variant-01
--project <project>` restores it. The CLI form is `timelines duplicate
<timeline> --project <project> --slug variant-01`.

## Direct CLI and the sheet

```bash
python3 -m astrid timelines edit <timeline> --project <project> \
  --add-layer media --layer-asset /path/to/video.mp4 --on 0 --for 3 --track V1 --id opening \
  --idempotency-key first-video-42 --json
python3 -m astrid timelines edit <timeline> --project <project> \
  --clip parent/clip:opening --on 1.5 --idempotency-key move-42 --json
python3 -m astrid timelines edit <timeline> --project <project> \
  --clip parent/clip:opening --trim-in 0.5 --trim-out 2.5 \
  --idempotency-key trim-42 --json
```

These three forms commit directly; each is one commit and one undo.
`parent/clip:<id>` addresses a clip by its `show` id (`parent/clip:` is
literal); `--on` is timeline seconds and `--trim-in`/`--trim-out` are file
seconds. A still image has no source window; change its length with `--for`.
Add audio and text with a Python function (`--add-media` is the legacy draft
verb). Symbolic moments and the other `edit` flags still edit a detached
working copy; prefer a Python function for those changes.

For a table of cuts, `timelines show <timeline> --as sheet --native --range
c10..c12 > cut.sheet`, edit the rows, then `timelines apply <timeline>
cut.sheet --idempotency-key sheet-42`. Supported clip and track rows commit
natively; anything else is refused rather than half applied.

## Results and errors

A commit result has `data.status` (`accepted`, `merged`, `rebased`, `preview`) and
`receipt.receipt_id`. CLI `--json` wraps the same data in
`ok`/`data`/`error`/`receipt`/`idempotency_key`. Report the label, receipt,
imports and how to undo.

Every refusal should read as a headline (what went wrong), a detail (where:
the clip, time or receipt) and a fix (what to do next). Ambiguous names list
their candidates. `CommitConflict` is a `TimelineEditError`; its `failure`
carries Runtime's `message` and `details` (including `status` and the
`current` head on a clash). Other `TimelineEditError`s mean nothing landed,
except one saying no reply could be verified: repeat that call with its key.
Never describe an unknown outcome as a failure, a retry as a redo, or a
preview as reserved.

## Looking at the result

Structure and timing can be read back; appearance cannot. Unless the person
asks for frames or a render, give them the editor link and the times to check,
and say the look is unverified. The link is
`https://astrid.haus/tools/video-editor?project=<project-id>&timeline=<timeline-id>`:
`<project-id>` is the `project_id` that `projects show <project> --json` prints
(`<project>` may be the slug), and `<timeline-id>` is the `timeline_id` that
`timelines list --project <project> --json` prints. When they ask, the frame commands are in
[render, inspect, transfer and worlds](../astrid/packs/rendering/docs/references/render-inspect-transfer.md).
The final perception guidance (project P) will replace this section.

## Not possible yet

- Public beat list (only beats near one word); a CLI word-anchor adapter.
- Changing an existing clip's text in place (`clip.set(text=...)`): not
  available.
- Ripple on one track only.
- Grouping existing clips into a shot; regrouping beyond `timelines restructure`.
- Cross-timeline transactions and group undo.
- Aspect-ratio variants.
- A portable `--changes` file for the CLI.
- Creating a timeline and its content in one public call: create with
  `timelines create`, then add the content in one commit.

## Checks and the journal

```bash
python3 -m astrid timelines check <timeline> --project <project> --sweep
python3 -m astrid timelines log <timeline> --project <project> --verify --json
```

`check --sweep` validates a saved working copy and compares bounded samples across every cut. Sweep status is 0 for expected results, 1 for unexpected picture changes, and 2 when capture was not made; a validation failure returns 1. Without a saved working copy, `check` prints a checkout hint and does not sweep. `--no-sweep` suppresses it. These bounded samples do not prove every-frame or audio identity.

`log --verify` checks the journal hash chain. A broken chain returns 1; JSON reports `error.code: validation_error`. A missing journal is reported as “no journal yet” and returns 0. Journal queries accept `--since`, `--actor`, `--op`, `--cut`, `--clip`, and `--outcome`; a query validation error is printed to stderr and returns 2.

## Shared timeline and shot contracts

`@banodoco/timeline-schema` owns `TimelineConfig` and timeline clip semantics.
Workspace Runtime's `contract/schemas/shot-composition.json` owns the outer
shot publication envelope (revision identity, head/CAS, dependencies, and
occurrences) and references the generated canonical config schema for nested
timelines. Consumers project these contracts into their local views; they do
not define a competing `TimelineConfig`.

Visible clip duration is the selected source duration divided once by positive
`speed`: `hold`, transport `duration_ms`, or source trim `to - from` supplies
the source duration. Parent occurrence `duration_ms` caps the resulting visible
interval. Source trims remain in source units, and the renderer owns final frame
rounding against the shared conformance vectors.

## Opening sources and returned artifacts

Keep these identities distinct: a clip's `asset`/`asset_id` is a timeline-local
registry alias; a managed media ID identifies a project media record; a
`source_object_id` identifies managed bytes; an artifact handle identifies a
returned view or run output. Never pass a display alias to an API that asks
for a managed ID, or construct an artifact URL from an alias/digest.
For a returned, non-null source object, `client.media.read_bytes(source_object_id)`
retrieves its bytes; media metadata alone does not prove that they were viewed.
