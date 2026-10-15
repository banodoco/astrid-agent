"""Explicit external checkpoints, with no gesture inference or document rebase.

External documents are authoritative snapshots. Relation-sensitive raw changes
need an explicit choice; pending authored commands use the sole rebase runner.
"""
from __future__ import annotations

import copy
from dataclasses import asdict
from typing import Mapping

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline import commands, journal
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.revisions import Revision


class AmbiguousImport(TimelineEditError):
    def __init__(self, entities, detail, next_step):
        self.entities = tuple(entities)
        self.next_step = next_step
        super().__init__(f"import_external refused for {', '.join(entities)}: {detail}. Next: {next_step}")


def _related(clip):
    return bool(intent.on(clip.data) or intent.until(clip.data) or intent.formulas(clip.data)
                or intent.sequence(clip.data))


def validate_external(base, external):
    """Refuse §4.3's relation-sensitive choices, without classifying T1–T10."""
    from astrid.sdk.timeline.checkout import Checkout
    from astrid.core.timeline.compatibility import require_writable_candidate

    require_writable_candidate(external)
    old, new = Checkout(copy.deepcopy(base)), Checkout(copy.deepcopy(external))
    if any(base.get(key) != external.get(key) for key in ('project_id', 'timeline_id')):
        raise TimelineEditError("external revision belongs to another timeline; select its exact project and timeline")
    before = {commands.scoped_clip(old, c): c for c in old.clips()}
    after = {commands.scoped_clip(new, c): c for c in new.clips()}
    timed = []
    for sid in before.keys() & after.keys():
        a, b = before[sid], after[sid]
        clock = lambda c: (c.start, c.end, c.data.get('from'), c.data.get('to'), c.data.get('speed'))
        # Sub-frame clock noise carries no authored edit, but is kept verbatim
        # in an otherwise admitted external snapshot.
        values = zip(clock(a), clock(b))
        if any(abs(x-y) >= .5 / old.fps if isinstance(x, (int, float)) and isinstance(y, (int, float))
               else x != y for x, y in values):
            timed.append(sid)
    tied = [sid for sid in timed if _related(before[sid])]
    if len(tied) > 1:
        raise AmbiguousImport(tied, "timing changed on multiple relation-tied clips without gesture annotations",
                              "re-save one gesture at a time, or record explicit anchor/offset commands")
    for sid in sorted(before.keys() - after.keys()):
        if old._is_picture(before[sid]):
            raise AmbiguousImport([sid], "a picture was deleted", "choose lift the cut or leave it empty, then record that command")
    for sid in timed:
        a = before[sid]
        if intent.sequence(a.data):
            raise AmbiguousImport([sid], "a sequence step changed timing", "choose detach / keep the sequence, adjust its offset, or re-anchor explicitly")
        if a.track == 'music' and old.music == 'keep_end':
            raise AmbiguousImport([sid], "the keep-end music bed changed timing", "choose detach / keep the bed, adjust its offset, or re-anchor explicitly")
    added_tied = [sid for sid in after.keys() - before.keys() if _related(after[sid])]
    if timed and added_tied:
        raise AmbiguousImport(sorted(added_tied), "a drag may have duplicated a ripple of relation-tied content",
                              "choose detach / keep, adjust offset, or re-anchor; re-save one gesture at a time")
    for sid in tied:
        a, b = before[sid], after[sid]
        explicit = lambda c: (intent.on(c.data), intent.until(c.data), intent.for_s(c.data))
        if explicit(a) == explicit(b):
            raise AmbiguousImport([sid], "raw timing changed while its relation stayed fixed (including overlap trims)",
                                  "choose detach / keep, adjust offset, or re-anchor with an explicit command")
    for sid, clip in sorted(after.items()):
        for text in (intent.on(clip.data), intent.until(clip.data)):
            if text:
                try:
                    commands.capture_binding(new, text, clip, {})
                except TimelineEditError as exc:
                    raise AmbiguousImport([sid], f"its relation cannot bind: {exc}",
                                          "select a surviving anchor and re-anchor explicitly, or detach the relation") from None
    for sid in sorted(before.keys() & after.keys()):
        a, b = before[sid], after[sid]
        for path, formula in intent.formulas(a.data).items():
            from astrid.sdk.timeline.doc import _get_path
            if (formula == intent.formulas(b.data).get(path)
                    and _get_path(a.data, path) != _get_path(b.data, path)):
                raise AmbiguousImport([sid], "a mark-formula value changed while its relation stayed fixed",
                                      "adjust the formula offset, detach it, or select a new mark explicitly")


def checkpoint(tl, *, document, provenance, base_digest):
    """Runner handler. The supplied exact-base guard also covers non-entity data."""
    if journal.document_digest(tl.document()) != base_digest:
        raise commands.CommandError("external checkpoint base changed; reload and import the exact revision again")
    if not isinstance(provenance, Mapping) or provenance.get('adapter') not in {'external', 'hand-edited-draft'}:
        raise commands.CommandError("external checkpoint needs original revision provenance")
    if not provenance.get('revision_id'):
        raise commands.CommandError("external checkpoint needs the original revision id")
    if not isinstance(provenance.get('base_document'), Mapping):
        raise commands.CommandError("external checkpoint needs its exact preceding document")
    if (document.get('base_parent') or {}).get('revision_id') != provenance['revision_id']:
        raise commands.CommandError("external checkpoint revision does not match its original document; reload that exact revision")
    validate_external(provenance['base_document'], document)
    tl.bundle = copy.deepcopy(document)
    tl._refresh()


def import_external(tl, base, external, revision: Revision, *, pending=(), adapter='external'):
    """One checkpoint, then pending commands rebased/replayed in the same transaction.

    All preflight refusals leave the local draft and journal untouched. ``base``
    is the exact document preceding this revision, never a guessed last head.
    No Runtime publication is performed.
    """
    from astrid.sdk.timeline.rebase import rebase
    from astrid.sdk.timeline.transaction import edit_transaction

    validate_external(base, external)
    pending = tuple(pending)
    if pending:
        original = rebase(pending, base)
        if original.conflicts or original.document != tl.document():
            raise commands.CommandError("pending commands do not reproduce this draft from the exact base; reload the original commands")
    elif tl.document() != base:
        raise commands.CommandError("draft has pending edits; supply its original recorded commands and exact base")
    rebased = rebase(pending, external) if pending else None
    if rebased is not None and rebased.conflicts:
        raise commands.CommandError(f"pending commands conflict: {rebased.conflicts}; next: {rebased.next_step}")
    provenance = dict(adapter=adapter, revision_id=revision.id, revision=asdict(revision),
                      base_document=copy.deepcopy(base))
    with edit_transaction(tl, 'import_external', check=False, quiet=True) as tx:
        tx.canonical = True
        commands.execute(tl, dict(op='timeline.import-external', select=None,
                         args=dict(document=copy.deepcopy(external), provenance=provenance,
                                   base_digest=journal.document_digest(tl.document()))))
        for call in (rebased.plan.calls if rebased is not None else ()):
            commands.execute(tl, call)
    return tx.plan


def find_external(base, head, revisions, *, imported=()):
    """Walk exact ancestry, oldest first. Id prefixes and source labels are not evidence."""
    if base == head:
        return []
    by_id = {row.id: row for row in revisions}
    duplicates = {row.id for row in revisions if sum(r.id == row.id for r in revisions) > 1}
    out, seen = [], set()
    current = head
    while current != base:
        if current in seen or current not in by_id:
            raise TimelineEditError("external revision ancestry is unavailable; list exact revisions back to the draft base")
        seen.add(current)
        if current in duplicates:
            raise TimelineEditError("repeated revision publications need exact publication ancestry; select the original publication event before importing")
        row = by_id[current]
        # Runtime's semantic acceptance evidence carries the actual executed
        # commands/digests/toolchain. A publisher name alone proves nothing.
        evidence = row.evidence or {}
        semantic = all(key in evidence for key in ('commands', 'digests', 'toolchain'))
        if row.id not in imported and not semantic:
            out.append(row)
        if row.parent is None:
            raise TimelineEditError("external revision ancestry is unavailable; use an exact revision with its previous revision link")
        current = row.parent
    return list(reversed(out))


def unimported(tl, head, client):
    from astrid.sdk.timeline.revisions import RuntimeRevisions
    if tl.base_revision == head:
        return []
    store = RuntimeRevisions(getattr(client, 'timelines', client))
    rows = store.list(str(tl.bundle['project_id']), str(tl.bundle['timeline_id']), limit=200)
    return find_external(tl.base_revision, head, rows or [])


def remember_saved(path, text):
    """Keep the last saved bytes using the existing local snapshot format.

    The hidden subdirectory is outside both undo/redo stacks. It carries no
    new identity or journal schema and includes the pinned provenance verbatim.
    """
    import os
    from astrid.sdk.timeline import store
    folder = store._history_dir(path) / '.saved'
    temporary, _ = store._stage_step(folder, 'saved draft', text)
    os.replace(temporary, folder / 'current.json')


def load_hand_edit(tl):
    import json
    from pathlib import Path
    from astrid.sdk.timeline import store
    from astrid.sdk.timeline.checkout import Checkout

    path = Path(tl.path)
    saved = store._history_dir(path) / '.saved' / 'current.json'
    with store._file_lock(path):
        text = path.read_text(encoding='utf-8')
        if text != tl._disk:
            raise TimelineEditError('draft changed while loading; load it again before editing')
        if saved.is_file():
            _, before_text = store._read_step(saved)
        else:
            # Upgrade a legacy draft using its existing digest/journal first.
            # A verified runner save bypasses adaptation; only unrecorded bytes
            # are external. Never infer a last saved state from compiled clocks.
            destination = (journal.journal_path(str(tl.bundle.get('project_id', '')),
                           str(tl.bundle.get('timeline_id', ''))) if store._is_draft(path)
                           else path.with_suffix(path.suffix + '.journal.jsonl'))
            rows = [r for r in journal.read(destination) if r.draft == path.stem
                    and r.outcome == 'changed' and r.after]
            if rows and journal.verify(destination):
                raise TimelineEditError('draft journal verification failed; restore its verified history before importing')
            latest = rows[-1] if rows else None
            if latest is not None and latest.after == journal.digest(text):
                remember_saved(path, text)
                return tl
            before_text = None
            if latest is not None and latest.undo_step and latest.calls:
                undo = store._history_dir(path) / latest.undo_step
                if undo.is_file():
                    from astrid.sdk.timeline.rebase import rebase
                    _, previous = store._read_step(undo)
                    prior = Checkout({})
                    prior.bundle = prior._absorb(json.loads(previous))
                    prior._refresh()
                    replayed = rebase(latest.calls, prior.document())
                    if not replayed.conflicts:
                        from astrid.sdk.timeline.doc import _serialize
                        candidate = Checkout(replayed.document)
                        candidate._sync_film_length()
                        recovered = _serialize(candidate._annotated())
                        if journal.digest(recovered) == latest.after:
                            before_text = recovered
            if before_text is None:
                from astrid.sdk.timeline_cuts import base_bundle
                from astrid.sdk.timeline.doc import _serialize
                # Pinned provenance is an exact base, never the current snapshot
                # with an old revision id pasted over it.
                if latest is not None:
                    raise TimelineEditError('last saved draft cannot be recovered exactly; restore that saved file or reapply edits to a fresh checkout')
                if not isinstance(tl.bundle.get('base_parent_payload'), Mapping):
                    return tl
                before_text = _serialize(Checkout(base_bundle(tl.document()))._annotated())
        base = Checkout({})
        base.bundle = base._absorb(json.loads(before_text))
        base._refresh()
        if base.document() == tl.document():
            return tl
        for key in ('project_id', 'timeline_id', 'base_parent', 'base_parent_payload', 'base_placements', 'source_mapping'):
            if base.bundle.get(key) != tl.bundle.get(key):
                raise TimelineEditError(f'hand-edited draft changed pinned {key}; restore its original provenance and load again')
        revision = Revision(base.base_revision, 'other', None, base.base_revision,
                            'hand-edited draft', provenance=dict(path=str(path), digest=journal.digest(text)))
        plan = import_external(base, base.document(), tl.document(), revision, adapter='hand-edited-draft')
        base.path, base._disk = path, text
        base._pending_calls = list(plan.calls)
        # The import undoes to the exact last saved document, not to the raw
        # hand edit which happened to occupy the file when load started.
        base._external_before = before_text
        base._save_locked(path, force=False)
        base.__dict__.pop('_external_before', None)
        return base
