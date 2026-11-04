# M2 completion auto-placement (step 1)

Restores the lost "anchor-after / absent-anchor fallback" completion placement for
an exact occurrence binding, image and video, from existing primitives only.
Agent unit candidate: not Runtime or App qualification. Step 2 is not done here.

## Contract

`astrid.sdk.timeline.native_auto_placement.auto_place_completion(bundle, owner,
clip_id, media, *, ownership, verified_media, anchor=None, duration=None)` is pure.
It reads the caller's original bundle and returns
`{'commands': [one ordinary EditCommand], 'capture': {...}}`.

* `owner`: `occurrence:<raw ID>` with `ownership='occurrence'`. Parent ownership,
  `shared`, display IDs and a shot shared by another occurrence refuse
  (`unsupported_ownership`). Parent-owned completion needs parent-media birth
  (`clip.add-media`), which this stack does not have.
* `clip_id`: caller-reserved raw child ID (a replacement needs a new one).
* `media`: the exact five-field pin of the **completed variant**, with its
  Runtime-verified entry in `verified_media`. The gallery primary and the latest
  variant are never consulted. Kind comes from the verified `media_type`.
* `anchor`: raw clip ID in the same occurrence, or `None`.

Emitted command: image -> `placement.create` (`hold` 5); video ->
`occurrence.add-media` with `{from: 0, to: d, hold: d, speed: 1}` where `d` is
`duration` (supplied by the caller) or 5 when none is supplied, never the full media
length. `d` may not exceed the verified duration, so a source shorter than 5 s refuses
`unsupported_timing`. The image hold ignores `duration`.
These are the completion defaults, kept distinct from the shared 4 s hold (step 2).

## Start rule

Frames at the pinned rational fps, intervals half-open, using the same
`clip_end_frame` as the compositor (`start + max(1, round(duration * fps))`).
The intent saved at submit time (main's behaviour: old intent wins) is used as given;
`preferred_track`, `fallback_at` (global seconds) and `fallback_track` are builder arguments.

* Anchor present: start = anchor end frame, on `preferred_track` (else the occurrence lane).
* Anchor absent (no id, or no such clip in the occurrence): start = the saved
  `fallback_at` on `fallback_track` (else the occurrence lane).
* Only with no saved time: start = lane end, the greatest end frame of the occurrence's own
  clips on the lane; an empty lane starts at the occurrence start.
* A saved track that no longer exists refuses (`dependency_changed`): the builder does not
  move the item to another lane. The caller shows "not placed" or re-submits.
* A present anchor with unusable timing refuses. It never falls back.

The start is quantized once, exactly as `placement.create` does, and written as a
literal. New clip goes on the occurrence lane. Result must fit the occurrence
(`at + hold <= duration`) or the existing `occurrence_boundary` refusal applies.

## Capture contract and replay

`capture` = owner, clip, kind, media pin, `anchor {id, state: present|absent}`,
`resolution` (`anchor-after|fallback-at|lane-end`), `lane`, `consumed`, fps, `start_frame`,
`on`, `seconds`. It explains the choice for the App's persisted continuation.
The command's literal start is what lands, and every clip row the choice read is a guard:

* `consumed` is the list of raw clip IDs whose rows the builder read (anchor; every clip for a
  lane end; none for a saved `fallback_at`). It travels as the optional `consumed` arg of
  `placement.create` and `occurrence.add-media`; the op reads each row inside the recipe
  recorder, so each is a recorded read guard, and an ID outside the occurrence refuses.
* an exact retry of the same request (same head) replays to identical records; inverse is exact;
* if a consumed clip *moved or changed* afterwards (anchor start 330 vs 360, lane end 450 vs 480,
  shared append 510 vs 540, occupied start 391, anchor lane), replay conflicts
  (`dependency_changed`) instead of landing at the stale position. The caller rebuilds on the
  new head, which re-reads and records the new values;
* if a clip was *added, deleted or reordered*, replay refuses via the existing
  collection-membership guard; a rebuild records `absent` or the new lane end;
* a saved `fallback_at` consumes nothing, so a sibling moving does not change the approved start;
* both births (`placement.create` and every `occurrence.add-media` branch, including videos
  and images off the home lane) check the exclusive owner at replay, so a retargeted
  occurrence cannot expose one birth through two scopes (replay refuses `selection_changed` on the
  recorded `placement.owners` selection; a fresh capture refuses `unsupported_ownership`);
* undo is the existing strict ABA of create/add-media (any intervening write to the
  created clip refuses), redo follows the removal recipe.

## Evidence

`tests/editing_conformance/test_recipe_m2_auto_placement.py`, golden
`fixtures/recipe_m2_auto_placement.json` (keys `image-anchor`, `image-absent`,
`video-anchor`, `video-absent`: commands, capture, created child row). Covers
anchor present/absent, empty lane, defaults and supplied/over-long video duration,
exact variant binding, shared/parent/unbound refusal, unverified/audio media,
unusable anchor timing, anchor moved/deleted replay, guarded undo/redo and the
provider undo/redo through `MediaRealm`.

## Corrected guards (2026-10-20)

Replaces the two former known gaps. `occurrence.add-media` now checks the exclusive owner
(`_exclusive`, a `placement.owners` selection) on every branch, which adds that selection to
the goldens `recipe_m2_occurrence_media.json` (add) and `recipe_m2_placement_create_unplace.json`
(create). The optional closed `consumed` arg (schema `OccurrenceAddMediaArgs`,
`PlacementCreateArgs`) is the only schema change; the vendored Runtime client types and Runtime
artifacts still need their normal regeneration. Tests that used to assert "replay keeps the
recorded start after the anchor/shot moved" now assert the conflict and a corrected rebuild
(old -> new).

## Step 2 needs (out of scope)

Shared 4 s image/video hold; shared append (after the group's greatest extent,
numeric fps default 30, empty group at 0); lane inheritance (shot-group lane, else
first group's lane, else first track, else `V1`); start-frame-only collision shift;
canonical pool membership with promote / unplace-to-pool (`keepAsPooled`);
legacy pinned-group migration; parent-owned completion (`clip.add-media`); Runtime
schema/provider pair for `placement.create` and the video row; App durable
continuation (task/attempt guards from Agent/Runtime, reserved IDs, verified pin).


# Shared auto-placement (step 2)

`auto_place_shared(bundle, owner, clip_id, media, *, ownership, verified_media, frame=None,
anchor=None, pooled=False)` in the same module; pure, one ordinary command plus a capture.
Owner is the canonical occurrence that exclusively owns its shot (a legacy group after
`timeline.migrate-groups`). A shot shared by two occurrences refuses `unsupported_ownership`;
non-empty `pinnedShotGroups` refuses `migration_needed`; display IDs never identify an owner.

* Hold: 4 s for image and video (completion stays 5 s). Image -> `placement.create`
  (or `occurrence.add-media` when the lane is not the occurrence lane); video ->
  `occurrence.add-media` with window `0..4`. The legacy video hold ignored media length;
  a native row authors the window, so media shorter than 4 s refuses.
* Append (`frame=None`): after the greatest end frame over ALL clips of the shot (any lane);
  empty shot -> occurrence start.
* Explicit `frame`: clamped at 0, then +1 while a clip START frame in the shot is equal
  (capture `skipped`); interval overlap is allowed.
* Lane: the anchor's lane when the anchor is in the shot, else the occurrence lane. The
  anchor never moves the start. Capture records anchor state, lane, extent / skipped frames.
* Replay/inverse are exact (literal start). Every clip row of the shot is a `consumed` guard
  (append extent, occupied starts, anchor lane): a changed clip conflicts at replay, a deleted
  one refuses via the membership guard, and a rebuild records the new values.
* The birth handler records `occurrence:<id>/source` at `app.astrid.pool` and `parent` at
  `config.pinnedShotGroups`, including absence, through the existing recorder. A concurrent
  pool add conflicts even when the exact alias was already bound; the original request never
  silently promotes media added after capture. Changed legacy eligibility also conflicts.
  A conflict keeps the original request for review; a new capture is a separate decision.
  These field reads conservatively apply to all `occurrence.add-media` births, including
  `placement.create` and completion, without changing their arguments or stable-head behavior.
  Changes to another owner's pool or unrelated source/parent fields remain disjoint.
* `unplace_shared(..., keep_as_pooled=False)` = `placement.unplace` (image only; binding kept).

Evidence: `test_recipe_m2_shared_auto_placement.py`, golden `recipe_m2_shared_auto_placement.json`.
App RED parity met Agent-side: 4 s video append (frame 120 at 30 fps), group-lane inheritance,
equal-start shift 30 -> 31 with overlap allowed. Pool membership follows (`shot.pool`).

## Shot pool (`shot.pool` v1)

Canonical home: an ordered list of registry asset aliases at `app.astrid.pool` of the shot's
internal timeline (the same `app.astrid` namespace clips use for authored intent). Members are
bound in the shot registry exactly like placed media, so the dependency manifest pins them.

* **Absent vs empty.** Absent = never authored here; `[]` = authored empty pool. `remove` of the
  last member writes `[]`, never clears the key. Inverse of the first `add` restores absence.
* **Clear policy.** Only an explicit `remove` shrinks the pool. Placing, unplacing or deleting a
  clip never touches it, except promote (below).
* **Op.** `shot.pool` v1, select `occurrence:<id>` (exclusive owner, like `placement.create`),
  args `{"action": "add"|"remove", "media": <five-field pin>}`, closed. `add` refuses a member
  (`identity_collision`) or an alias a shot clip already uses (`identity_conflict`; the clip
  `asset`/`asset_id` are read-guarded), binds the exact media (existing equal binding is fine),
  appends. `remove` refuses a non-member, guards the exact binding and keeps it. A malformed
  pool (not a list of strings) refuses `unsupported_pool`. All `shot.pool` executions also
  record `parent.config.pinnedShotGroups`, covering eligibility of shared pool-only requests.
  This is a conservative replay guard; native pool operations add no migration semantics.
* **Ordered semantics.** The write replaces the whole list, so the recorded before/after make
  undo restore the exact original position and redo the exact result; ABA is the usual guard on
  the after-state (an intervening write to the pool path, even an equal one, refuses undo).
  Rebase replays the captured intent: edits disjoint from this pool (other clips, other shots'
  pools, the parent) rebase; a concurrent change to the same pool is a real conflict.
* **Builder.** `auto_place_shared(pooled=True)` = `shot.pool add` (no frame/anchor). Placing a
  media whose alias is pooled promotes it: birth command + `shot.pool remove` in one batch
  (capture `promoted: true`). `unplace_shared(keep_as_pooled=True, media=, verified_media=)` =
  `placement.unplace` + `shot.pool add`; the caller supplies the exact pin (the registry has no
  catalog digest). Video keepAsPooled stays out with `placement.unplace` (image only).
* **Legacy `poolGenerationIds`.** `timeline.migrate-groups` (on the group branches) keeps them
  only in `provenance.group_migration.legacy_group` and leaves `app.astrid.pool` absent;
  provenance is never rewritten. Adoption is an explicit recorded step: one `shot.pool add` per
  legacy generation, with the pin the caller resolved (alias `gen:<id>` in App). The duplicate
  guard makes adoption idempotent-refusing, and each add is a guarded, invertible command.
  Making the migration emit those adds itself is a follow-up on the group branch.

Evidence: `test_recipe_m2_shot_pool.py`, golden `recipe_m2_shot_pool.json`; the two former strict
xfails in `test_recipe_m2_shared_auto_placement.py` now pass. Runtime artifact regeneration for
the new args is a follow-up.

## BLOCKED (no existing writer; none invented)

1. ~~Pool membership~~ — done above.
2. **Append beyond the occurrence window.** Migration sets `duration_ms` to the member span, so
   every append refuses `occurrence_boundary`. Proposed closed op `occurrence.set-extent` v1,
   select `occurrence:<id>`, args `{"duration_ms": <positive number>}`, guarded on the old
   value; the builder would emit it with the birth command. Tests use a widened window.
3. Group migration (`timeline.migrate-groups`) lives on `tl/agt-m2-group-*` branches, not in this base.

Still open (separate from the guards above): parent-owned completion births (missing parent
births, `clip.add-media`), append growth past the occurrence window (item 2), video unplace
(`placement.unplace` is image only), and the omitted opacity/volume/manual-track defaults of the
old completion add. Shared placement still uses a fixed 4 s and pinned rational fps, not the old
numeric fps default.
