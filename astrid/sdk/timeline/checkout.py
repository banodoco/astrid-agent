"""The Checkout class: one editable working copy (assembled from the mixins), open/save/undo/draft."""
from __future__ import annotations

import contextlib
import copy
import json
import os
from pathlib import Path
from typing import Any, Mapping

from astrid.core.timeline import astrid_ns as _ns
from astrid.sdk import timeline_intent as intent
from astrid.core.timeline.cuts import (
    bundle_fps,
)

from astrid.sdk.timeline import journal as _journal
from astrid.sdk.timeline import store as _store
from astrid.sdk.timeline._common import (TimelineEditError, _Suggest)
from astrid.sdk.timeline.check import (_CheckMixin, describe_changes)
from astrid.sdk.timeline.doc import _serialize
from astrid.sdk.timeline.edits import _EditMixin
from astrid.sdk.timeline.lookup import _LookupMixin
from astrid.sdk.timeline.model import (Clip, Cut, StepSequence, Voice)
from astrid.sdk.timeline.publish import _PublishMixin
from astrid.sdk.timeline.reflow import _ReflowMixin
from astrid.sdk.timeline.transaction import edit_transaction


# ------------------------------------------------------------------ checkout

class Checkout(_LookupMixin, _EditMixin, _ReflowMixin, _CheckMixin, _PublishMixin, _Suggest):
    """A complete, editable copy of one timeline head (parent → shots → internal timelines)."""

    bundle: dict[str, Any]

    def __init__(self, bundle: dict[str, Any], *, path: Path | None = None):
        intent.migrate(bundle)  # a working copy holds Astrid intent under app.astrid only (old places move in; reads stay dual)
        from astrid.sdk.timeline_restructure import flatten

        bundle = flatten(bundle)  # a chapter film (shot per chapter + film-level clips) is edited as ONE flat shot; publish splits it again
        self.bundle = bundle
        self.path = path
        self.fps = bundle_fps(bundle) or 30.0
        self.notes: list[str] = []
        self.report: list[str] = []  # what the last re-flow did, in plain words
        self.head_moved_to: str | None = None  # set by Checkout.draft when a reused copy's base is behind the head
        self._mcache: dict[str, Any] | None = None
        # Re-flow keeps the music's ending: the bed is cut at a bar with a crossfade so it ends on the film's
        # end ("keep_end"). "plays_on" keeps the bed whole, as it was before (the picture moves against it).
        self.music = "keep_end"
        self._keep_inside = False  # a removal keeps the clips inside the window (as orphans) instead of deleting them
        self._kept: list[str] = []
        self._outside: list[str] = []
        self._journal: list[tuple[str, str | None]] = []  # (label, the document before that edit), one per edit
        self._helper_calls: list[dict[str, Any]] = []
        self._local_scalar_edits: list[tuple[str, str]] = []
        self._edit_depth = 0
        self._open_edit: Any = None  # the Edit of the tl.edit now open (nested edits join it)
        self._orphan_reason = "its line was removed"
        self._disk: str | None = None  # the file text this handle loaded (or last saved): what another writer may change
        self.merged: list[str] = []      # what the last save merged in from another writer, in plain words
        # An explicitly connected checkout retains the existing target/client
        # binding for read-only managed Scene diagnostics.  Detached checkouts
        # leave these unset and keep their local check behaviour.
        self._scene_client: Any = None
        self._scene_project: str | None = None
        self._scene_timeline: str | None = None

    def _refresh(self) -> None:
        """Rebuild derived checkout state after an edit transaction restores its bundle."""
        self.fps = bundle_fps(self.bundle) or 30.0
        self._mcache = None

    # ---- open / save ------------------------------------------------------
    @classmethod
    def attach(cls, session):
        """Edit a TimelineSession's original base and pending intent.

        Bounded literal clip verbs queue durably before updating the preview.
        Use ``flush()`` to accept them; ``publish(message)`` also accepts one
        captured parent command (including selected PARAM) when the session
        explicitly names its draft's outbox path.
        Unsupported verbs refuse explicitly.
        Detached ``open``/``load``/``draft`` retain their existing behavior.
        """
        from astrid.sdk.timeline.native_checkout import AttachedCheckout

        return AttachedCheckout(session)

    @classmethod
    def open(cls, project: str, timeline: str, *, revision_id: str | None = None,
             publication_id: str | None = None, client: Any = None) -> "Checkout":
        """Check out the current head (read-only until ``publish``)."""
        bundle = _store.fetch_bundle(project, timeline, revision_id=revision_id,
                                     publication_id=publication_id, client=client)
        intent.migrate(bundle)  # the fetched copy is ours; the published revision is never rewritten
        checkout = cls(bundle)
        checkout._scene_client = client
        checkout._scene_project = project
        checkout._scene_timeline = timeline
        return checkout

    @staticmethod
    def _pin_initial_base(bundle: dict[str, Any]) -> None:
        """Keep the exact fetched content as provenance for this new working copy."""
        parent = bundle.get("parent")
        if not isinstance(bundle.get("base_parent_payload"), Mapping) and isinstance(parent, Mapping):
            bundle["base_parent_payload"] = copy.deepcopy(dict(parent))

        placements = bundle.get("placements")
        source_mapping = bundle.get("source_mapping")
        mapped_placements = source_mapping.get("placements") if isinstance(source_mapping, Mapping) else None
        if not isinstance(mapped_placements, Mapping) and not isinstance(bundle.get("base_placements"), Mapping):
            rows = placements if isinstance(placements, list) else []
            bundle["base_placements"] = {
                str(row.get("occurrence_id") or row.get("id") or index): copy.deepcopy(dict(row))
                for index, row in enumerate(rows) if isinstance(row, Mapping)
            }

        shots = bundle.get("shots")
        if not isinstance(shots, Mapping):
            return
        for shot in shots.values():
            if not isinstance(shot, dict):
                continue
            payload = shot.get("payload")
            internal = shot.get("internal_timeline") or shot.get("timeline")
            if not isinstance(shot.get("base_payload"), Mapping) and isinstance(payload, Mapping):
                shot["base_payload"] = copy.deepcopy(dict(payload))
            if not isinstance(shot.get("base_internal_timeline"), Mapping) and isinstance(internal, Mapping):
                shot["base_internal_timeline"] = copy.deepcopy(dict(internal))

    @classmethod
    def load(cls, path: str | Path) -> "Checkout":
        """Open a checkout file (``timelines checkout`` or ``save`` wrote it). Applies `_timeline` edits."""
        path = Path(path)
        with _store._file_lock(path):
            _store._reconcile_restore(path)
            return cls._load_locked(path)

    @classmethod
    def _load_locked(cls, path: Path) -> "Checkout":
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text)
        tl = cls({}, path=path)
        tl.bundle = _absorb_draft(tl, raw)
        tl.fps = bundle_fps(tl.bundle) or 30.0
        tl._disk = text
        return tl

    HISTORY = 60  # edits of a working copy kept for undo

    SAY_SAVED = True  # a script's tl.save() prints one line (the CLI verbs print their own report)

    def save(self, path: str | Path | None = None, *, force: bool = False, quiet: bool | None = None) -> Path:
        """Write the checkout: content pretty-printed, provenance compact, both clocks on every clip.

        A working copy keeps one undo step per EDIT made since the last save (each verb, each
        API call such as ``clip.on(...)``, each applied sheet), so ``undo`` goes back one edit.

        Two writers on one working copy never lose each other's edits: if the file changed since
        this handle loaded it, the save is a three-way merge (what you loaded → your edit, onto
        what is there now), like publish. Edits to different clips merge (``self.merged`` says
        what came in); the same clip changed by both refuses, unless ``force`` (yours wins)."""
        target = Path(path or self.path or "timeline.checkout.json")
        sheet = _pending_sheet(self, target)
        with _store._file_lock(target):
            if sheet and target.is_file() and target.read_text(encoding="utf-8") != self._disk:
                raise TimelineEditError("retained sheet draft changed on disk; reopen it before saving")
            saved = self._save_locked(target, force=force)
        if sheet:
            native, session, text = sheet
            native.apply(text, session=session, dry_run=False)
            self._retire_draft_history()
        if (self.SAY_SAVED if quiet is None else not quiet) and not _store._cli_running():
            name = getattr(self, "draft_name", None)
            changed = len(self.edits().get("changes") or [])
            print(f"saved {'working copy ' + repr(name) if name else saved} · {changed} clip(s) changed vs published"
                  + (f" · {self.merged[0]}" if self.merged else "") + " · tl.check().brief() says what blocks publishing")
        return saved

    def _save_locked(self, target: Path, *, force: bool, record: bool = True) -> Path:
        _store._reconcile_restore(target)
        original_bundle = self.bundle
        self.merged = []
        if self._disk is not None and self.path and Path(self.path) == target and target.is_file():
            now = target.read_text(encoding="utf-8")
            if now != self._disk:
                self._merge_writer(target, now, force=force)
        self.notes.extend(self._sync_film_length())  # the film's length follows its content, before it is written
        document = self._annotated()
        if "source_mapping" in self.bundle:
            document["source_mapping"] = copy.deepcopy(self.bundle["source_mapping"])
        text = _serialize(document)
        staged: list[tuple[Path, Path]] = []  # undo steps: written under a temp name now, committed after the file
        clear_redo = False
        journaled_step: tuple[str, str, str] | None = None  # (label, previous text, step file name) of the step staged now
        if record and target.is_file() and _store._is_draft(target):
            previous = target.read_text(encoding="utf-8")
            journal = list(self._journal)
            if previous != text and not journal and not _same_document(target, previous, text):
                # edited without the API (by hand): one step. A pre-A1 file that differs only by the migration
                # (intent moved under app.astrid, derived fields dropped) is not an edit: no step, no record.
                journal = [("an edit", None)]
            if journal:
                # One undo step per save: everything journaled since the last save, whose "before" is the file as it was
                history = _store._history_dir(target)
                history.mkdir(parents=True, exist_ok=True)
                _store._discard_half_written(history)
                staged.append(_store._stage_step(history, _joined_label(journal), previous))
                journaled_step = (_joined_label(journal), previous, staged[-1][1].name)
                clear_redo = True
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, target)  # a reader never sees half a file
        except OSError:
            # A failed replacement must leave the original pending scalar
            # capture usable for retry, rather than turning the merge into intent.
            self.bundle, self._mcache, self.merged = original_bundle, None, []
            raise
        # A step is committed only now that the file it describes is in place. A crash before this point leaves a
        # temp step (ignored, and cleared by the next save), never a step whose state is the file on disk: that
        # would make the next undo a silent no-op.
        for step_tmp, step in staged:
            os.replace(step_tmp, step)
        if staged:
            for old in sorted(_store._history_dir(target).glob("*.json"))[:-self.HISTORY]:
                old.unlink()
        if clear_redo:
            import shutil

            shutil.rmtree(_store._redo_dir(target), ignore_errors=True)  # a new edit ends the redo chain
        self._journal = []
        self._local_scalar_edits = []
        self.path = target
        self._disk = text
        if journaled_step is not None:  # the journal: one record per undo step (the file and the step are committed now)
            label, before_text, undo_step = journaled_step
            try:
                clips, cuts = _clip_changes(before_text, text)
                _journal.record_working_copy(target, owner=self, kind="save", outcome="changed", label=label,
                                             before=_journal.digest(before_text), after=_journal.digest(text),
                                             clips=tuple(clips), cuts=tuple(cuts), undo_step=undo_step,
                                             calls=tuple(self._helper_calls))
            except Exception as exc:  # noqa: BLE001 - the save is committed; only its journal record is missing
                _journal.note_failure(self, exc)
        self._helper_calls = []
        return target

    def _merge_writer(self, target: Path, now: str, *, force: bool) -> None:
        """Another writer saved this file since we loaded it: put our edit on top of theirs."""
        import datetime as _dt

        mine = Checkout({}, path=target)
        base = _absorb_draft(mine, json.loads(self._disk or "{}"))
        theirs = _absorb_draft(mine, json.loads(now))
        when = _dt.datetime.fromtimestamp(target.stat().st_mtime).strftime("%H:%M:%S")
        snapshots = self._local_scalar_edits
        from ._command_state import equal

        bounded = bool(snapshots) and equal(json.loads(snapshots[0][0]), base) and all(
            left[1] == right[0] for left, right in zip(snapshots, snapshots[1:])
        ) and snapshots[-1][1] == _store._snapshot(self)
        merged, conflicts = _merge_local_draft(base, theirs, self.bundle, force=force, bounded=bounded)
        if conflicts and not force:
            raise TimelineEditError(
                f"not saved: this working copy was saved by another writer at {when}, after you opened it, and you "
                f"both changed {len(conflicts)} of the same thing(s): " + "; ".join(conflicts[:6])
                + (f"; … {len(conflicts) - 6} more" if len(conflicts) > 6 else "")
                + ". Open it again (their edit is there) and redo yours, or save(force=True) to overwrite theirs.")
        before = Checkout(copy.deepcopy(base))
        theirs_said = describe_changes(before, Checkout(copy.deepcopy(theirs)))
        self.bundle, self._mcache = merged, None
        self.merged = [f"merged with another writer's save at {when}: kept their {len(theirs_said)} change(s)"
                       + (" (yours won where you both changed something)" if conflicts else "")] + theirs_said[:8]

    def edit(self, label: str, *, dry_run: bool = False, check: bool = True, quiet: bool = False,
             reason: str | None = None):
        """`with tl.edit(label) as tx:`: everything inside is one undo step, one save, one change-list entry (API.md §5).

        An exception inside restores the document exactly and saves nothing. No change: no step, no save.
        ``dry_run=True`` reports and restores (nothing saved). ``tx.plan`` is filled for every transaction."""
        return edit_transaction(self, label, dry_run=dry_run, check=check, quiet=quiet, reason=reason)

    def undo(self, steps: int = 1, *, publications: list[str] | None = None,
             client: Any = None, **options: Any) -> list[str] | dict[str, Any]:
        """Undo unsent draft edits, or reverse accepted ``publications`` through Runtime.

        Accepted undo returns the command result and leaves this detached working copy
        untouched. Reopen the timeline to read its new head. Runtime guards the inverse
        against intervening writes; the local snapshots never restore accepted history.
        """
        if publications is not None:
            if steps != 1:
                raise TimelineEditError("publication undo cannot be combined with draft steps")
            return self._inverse_publications(publications, client=client, **options)
        if options:
            raise TimelineEditError("commit options require publications")
        return self._restore_draft(steps, redo=False)

    def redo(self, steps: int = 1, *, publications: list[str] | None = None,
             client: Any = None, **options: Any) -> list[str] | dict[str, Any]:
        """Redo draft edits, or reverse the accepted undo publication(s) through Runtime."""
        if publications is not None:
            if steps != 1:
                raise TimelineEditError("publication redo cannot be combined with draft steps")
            return self._inverse_publications(publications, client=client, **options)
        if options:
            raise TimelineEditError("commit options require publications")
        return self._restore_draft(steps, redo=True)

    def _inverse_publications(self, publications, *, client, **options):
        from astrid.sdk.timeline.sync import TimelineSession

        if client is None:
            from astrid.sdk import AstridClient

            with AstridClient.open_from_launcher(start_pack_host=False) as connected:
                return self._inverse_publications(publications, client=connected, **options)
        session = TimelineSession.open(self.bundle["project_id"], self.bundle["timeline_id"], client=client)
        return session.undo(publications, **options)

    def _retire_draft_history(self) -> None:
        """Acknowledged intent cannot become unsent again, even at a retained revision."""
        if self.path:
            import shutil

            target = Path(self.path)
            with _store._file_lock(target):
                _store._restore_marker(target).unlink(missing_ok=True)
                for folder in (_store._history_dir(target), _store._redo_dir(target)):
                    if folder.exists():
                        shutil.rmtree(folder)
        self._journal = []
        self._local_scalar_edits = []
        self.__dict__.pop("_exported", None)

    def _restore_draft(self, steps: int, *, redo: bool) -> list[str]:
        if not self.path:
            raise TimelineEditError("only a saved working copy can undo or redo draft edits; "
                                    "use publications for accepted edits")
        target = Path(self.path)
        source, destination = ((_store._redo_dir, _store._history_dir) if redo else
                               (_store._history_dir, _store._redo_dir))
        kind = "redo" if redo else "undo"
        with _store._file_lock(target):
            _store._reconcile_restore(target)
            start = current = target.read_text(encoding="utf-8")
            base = json.loads(start).get("base_parent")
            selected = []
            for step in reversed(sorted(source(target).glob("*.json"))):
                label, state = _store._read_step(step)
                # A publish/reset changes the base. Older snapshots are no longer
                # unsent intent and must never rewind that accepted boundary.
                if json.loads(state).get("base_parent") != base:
                    break
                selected.append((step, label, state))
                if len(selected) >= max(1, int(steps)):
                    break
            if not selected:
                raise TimelineEditError(f"nothing to {kind}: no unsent draft edit at this base; "
                                        "use publications for accepted edits")
            labels = []
            staged = []
            temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            try:
                for step, label, state in selected:
                    staged.append(_store._stage_step(destination(target), label, current))
                    current = state
                    labels.append(label)
                temporary.write_text(current, encoding="utf-8")
                _store._stage_restore(target, temporary, redo=redo,
                                      sources=[step for step, _label, _state in selected], staged=staged)
                os.replace(temporary, target)
            except BaseException:
                # A replace may have succeeded before the interruption was raised.
                # Leave that move recoverable, including equal-value/ABA drafts.
                if temporary.exists() or not _store._restore_marker(target).exists():
                    _store._restore_marker(target).unlink(missing_ok=True)
                    temporary.unlink(missing_ok=True)
                    for step_tmp, _step in staged:
                        step_tmp.unlink(missing_ok=True)
                raise
            # Keep every source until the draft and all reverse states are in
            # place. Failed writes/replaces leave the original undo chain intact.
            _store._reconcile_restore(target)
            self._reload()
        _journal.record_or_note(self, target, kind=kind, outcome="changed", label="; ".join(labels)[:160],
                                before=_journal.digest(start), after=_journal.digest(current))
        return labels

    def _reload(self) -> None:
        path = self.path
        if path is None:
            raise TimelineEditError("only a saved working copy can reload")
        fresh = Checkout._load_locked(path)
        self.bundle, self._mcache, self._journal, self._disk = fresh.bundle, None, [], fresh._disk
        self._local_scalar_edits = []

    @contextlib.contextmanager
    def step(self, label: str):
        """One undo step for everything inside (a verb that makes several changes is one edit)."""
        if self._edit_depth:
            yield self
            return
        before = _store._snapshot(self)
        self._edit_depth += 1
        try:
            yield self
        finally:
            self._edit_depth -= 1
        if _store._snapshot(self) != before:
            self._journal.append((label, before))

    # ---- working copy (draft) --------------------------------------------------
    @classmethod
    def draft(cls, project: str, timeline: str, name: str | None = None, *, client: Any = None, fresh: bool = False) -> "Checkout":
        """The timeline's working copy: the existing draft, or a new one checked out from the head.

        Drafts live in Astrid's data root (never a file you name). ``show``, ``lint``, ``diff``,
        ``visualize`` and ``render --draft`` read the same working copy; ``save()`` writes it back.
        """
        project_id, timeline_id, _head = _store.resolve_ids(project, timeline, client=client)
        name = name or _store.current_draft(project_id, timeline_id)
        path = _store.draft_path(project_id, timeline_id, name)
        if path.is_file() and not fresh:
            tl = cls.load(path)
            # A reused copy keeps the base it was made from; remember the head it no longer matches (no extra read).
            tl.head_moved_to = _head if _head and tl.base_revision != _head else None
        else:
            fetched = _store.fetch_bundle(project, timeline, client=client)
            intent.migrate(fetched)  # checkout: legacy intent -> app.astrid in the working copy (its pinned base copies are migrated too, so the move is not an edit; the published revision is untouched)
            cls._pin_initial_base(fetched)
            tl = cls(fetched)
            path.parent.mkdir(parents=True, exist_ok=True)
            tl.save(path)
            _journal.record_or_note(tl, path, kind="checkout", outcome="changed", label="checkout",
                                         after=_journal.digest(path.read_text(encoding="utf-8")))
        tl._scene_client = client
        tl._scene_project = project
        tl._scene_timeline = timeline
        _store.set_current_draft(project_id, timeline_id, name)  # commands that name no draft use this one
        tl.draft_name = name
        return tl

    def adopt(self, timeline: str, *, revision_id: str | None = None, client: Any = None) -> list[str]:
        """Make this working copy equal another timeline's head (or ``revision_id``): its cuts, narration
        (lines, takes, words), clips with all their intent, assets and the music's beats, chapters and slots.

        The working copy keeps its own timeline, shots and base, so ``changes()``, ``check()``, status and
        the three-way publish guard work as for any edit (one undo step). Returns what changed, in words."""
        from astrid.sdk.timeline_adopt import adopt_content, adopt_summary

        project = str(self.bundle.get("project_id") or "")
        from astrid.sdk.timeline_restructure import flatten

        source = flatten(_store.fetch_bundle(project, timeline, revision_id=revision_id, client=client))
        before = Checkout(copy.deepcopy(self.bundle))
        bundle, notes = adopt_content(source, self.bundle)
        self.bundle, self._mcache = bundle, None
        self.fps = bundle_fps(bundle) or self.fps
        self.notes.extend(notes)
        rev = str((source.get("base_parent") or {}).get("revision_id") or revision_id or "")
        self.report = adopt_summary(before, self, f"{timeline}" + (f"@{rev.removeprefix('authoring-parent-revision-')[:8]}" if rev else ""))
        self.report += [f"  note: {n}" for n in notes]
        return list(self.report)

    def discard(self) -> None:
        """Delete this working copy and its undo history (nothing published is touched)."""
        if self.path and Path(self.path).is_file():
            gone = Path(self.path)
            _journal.record_or_note(self, gone, kind="discard", outcome="changed", label="discard",
                                         before=_journal.digest(gone.read_text(encoding="utf-8")))  # before the file goes
            gone.unlink()
        if self.path:
            import shutil

            shutil.rmtree(_store._history_dir(Path(self.path)), ignore_errors=True)

    @property
    def base_revision(self) -> str:
        """The published revision this working copy started from."""
        return str((self.bundle.get("base_parent") or {}).get("revision_id") or "")

    def edits(self) -> Mapping[str, Any]:
        """What this working copy changes against its base (``diff_bundles`` shape, timeline seconds)."""
        from astrid.sdk.timeline_cuts import base_bundle, diff_bundles

        document = self.document()
        return diff_bundles(base_bundle(document), document)

    def changed_cut_ids(self) -> list[str]:
        """The cut ids whose clips this working copy changes (added, removed, moved or edited)."""
        from astrid.sdk.timeline_cuts import base_bundle

        before = Checkout(base_bundle(self.document()))
        old = {c.id: c for c in before.clips()}
        new = {c.id: c for c in self.clips()}
        cut_ids: set[str] = set()
        for cid in set(old) | set(new):
            a, b = old.get(cid), new.get(cid)
            if a is None or b is None or a.data != b.data or round(a.start * self.fps) != round(b.start * self.fps):
                for clip in (a, b):
                    if clip is not None and intent.cut_of(clip.data):
                        cut_ids.add(intent.cut_of(clip.data))
        order = [g["id"] for g in self._cut_groups()]
        return sorted(cut_ids, key=lambda c: order.index(c) if c in order else len(order))

    def log(self, **filters: Any) -> list[_journal.JournalRecord]:
        """The edit journal of this timeline (every working copy shares it), filtered: since, actor, op, cut, clip, outcome."""
        path = _journal.journal_path(str(self.bundle.get("project_id") or ""), str(self.bundle.get("timeline_id") or ""))
        return _journal.query(_journal.read(path), **filters)

    def changes(self, against: Mapping[str, Any] | None = None) -> list[str]:
        """What this working copy changes, in plain words (against its base, or another bundle).

        ``c30.cover  now holds until "Astrid"  93.400 → 93.500 s (+0.10 s)``. Clips that only
        moved because something before them moved are summed up in one line per shift."""
        from astrid.sdk.timeline_cuts import base_bundle

        before = Checkout(copy.deepcopy(dict(against)) if against is not None else base_bundle(self.document()))
        return describe_changes(before, self)


def _sheet_content(bundle):
    return Checkout(copy.deepcopy(bundle)).document()


def _sheet_session(tl, *, client=None, path=None, pending_only=False):
    """Reopen only this draft's retained outbox, without consulting a new head."""
    from astrid.sdk.timeline.outbox import Outbox
    from astrid.sdk.timeline.sync import TimelineSession, _connection

    target = path or tl.path
    if target is None:
        return None
    outbox = Outbox(Path(str(target) + ".outbox.json"))
    state = outbox.read()
    if state is None or "detached" not in state.get("sheet", {}) or (pending_only and not state["pending"]):
        return None
    client = client or tl._scene_client
    if client is None:
        raise TimelineEditError("retained sheet needs its original authenticated Runtime client")
    session = TimelineSession(client, outbox, _connection(client), tl.bundle["project_id"], tl.bundle["timeline_id"])
    with outbox.locked():
        session._state()  # checks realm, actor, project and timeline, including on retry
    return session


def _retain_sheet(tl, text):
    """Keep native source and legacy row bindings before returning editable text."""
    from astrid.sdk.timeline._command_state import equal
    from astrid.sdk.timeline.sync import TimelineSession, _connection
    from astrid.sdk.timeline_sheet import NativeSheet, SheetError

    client = tl._scene_client
    if tl.path is None or client is None or "structure_base" in tl.bundle:
        return
    try:
        _connection(client)
    except (AttributeError, TimelineEditError):
        return  # A local checkout without a verified target remains local.
    try:
        session = TimelineSession.open(tl.bundle["project_id"], tl.bundle["timeline_id"], client=client,
                                       outbox_path=Path(str(tl.path) + ".outbox.json"))
    except TimelineEditError as exc:
        if str(exc) == "timeline has no verified publication; editing is unavailable":
            return
        raise
    with session.outbox.locked():
        state = session._state()
        if state["pending"] or state["flight"] is not None:
            raise SheetError("flush original pending intent before exporting another sheet")
        bundle = session._capture_bundle(state)
        if bundle["base_parent"] != tl.bundle["base_parent"] or not equal(_sheet_content(bundle), _sheet_content(tl.bundle)):
            return  # Never certify a draft authored over a different/local basis.
        native = NativeSheet(bundle, basis=state["base"])
        bindings = {}
        for group in tl._cut_groups():
            for clip in group["clips"]:
                owners = [row for row in bundle["placements"] if row["shot_id"] == clip.shot_id]
                if len(owners) == 1:
                    bindings[group["id"] + "." + (intent.layer_of(clip.data) or clip.id)] = (
                        "occurrence:" + owners[0]["occurrence_id"] + "/clip:" + clip.id)
        state["sheet"] = {"bundle": native._bundle, "basis": native._basis,
                          "detached": {"text": text, "bindings": bindings, "checkout": tl.document()}}
        session.outbox.write(state)


def _stage_sheet(tl, text, parsed, *, client=None):
    """Save original captured duration intent before exposing its local preview."""
    from astrid.sdk.timeline._command_state import equal
    from astrid.sdk.timeline_sheet import NativeSheet, SheetError, _duration_sheet

    session = _sheet_session(tl, client=client)
    if session is None:
        return None
    tl._scene_client = session.client
    with session.outbox.locked():
        state = session._state()
        native = NativeSheet(state["sheet"]["bundle"], basis=state["sheet"]["basis"])
        artifact = state["sheet"]["detached"]
        translated = _duration_sheet(native, artifact, parsed)
        if translated is None:
            if state["pending"] or state["flight"] is not None:
                raise SheetError("flush original pending sheet intent before editing other cells")
            return None
        plan = native.preview(translated)
        if not plan.commands:
            if state["pending"]:
                raise SheetError("flush original pending sheet intent before editing other cells")
            return []
        if state["base"] != native._basis:
            raise SheetError("sheet no longer has its original authored basis; export again")
        if state["pending"] and not equal(state["pending"], plan.commands):
            raise SheetError("flush original pending sheet intent before editing other cells")
        candidate = Checkout(copy.deepcopy(plan.bundle))
        current = _sheet_content(tl.bundle)
        if not any(equal(current, _sheet_content(bundle)) for bundle in (artifact["checkout"], candidate.bundle)):
            raise SheetError("local draft differs from the retained sheet; keep it and export again")
        changes = describe_changes(tl, candidate)
        state["pending"] = plan.commands
        artifact["edited"] = text
        session.outbox.write(state)
    tl.bundle = candidate.bundle
    tl.bundle.get("source_mapping", {}).pop("_runtime_document", None)
    tl._refresh()
    return changes or ["sheet edit pending acceptance"]


def _pending_sheet(tl, target):
    from astrid.sdk.timeline._command_state import equal
    from astrid.sdk.timeline_sheet import NativeSheet, SheetError, _duration_sheet, parse_sheet

    session = _sheet_session(tl, path=target, pending_only=True)
    if session is None:
        return None
    with session.outbox.locked():
        state = session._state()
        if not state["pending"]:
            return None
        native = NativeSheet(state["sheet"]["bundle"], basis=state["sheet"]["basis"])
        artifact = state["sheet"]["detached"]
        text = _duration_sheet(native, artifact, parse_sheet(artifact["edited"]))
        plan = native.preview(text)
        if not equal(plan.commands, state["pending"]) or not equal(_sheet_content(tl.bundle), _sheet_content(plan.bundle)):
            raise SheetError("local draft differs from the original pending sheet intent")
        return native, session, text


def _absorb_draft(checkout, raw):
    # Immutable closure images are provenance, including their private keys.
    # The ordinary editable-document reader strips derived keys recursively.
    source = copy.deepcopy(raw.get("source_mapping"))
    bundle = checkout._absorb(raw)
    if source is not None:
        bundle["source_mapping"] = source
    return bundle


def _merge_local_draft(base, head, draft, *, force, bounded):
    """Replay local payload patches without publishing or manufacturing closure pins.

    Captured clip scalar edits guard the complete item, not just the changed leaf.
    Other transforms guard their whole payload, since a diff cannot capture reads.
    Provenance and membership must still describe the same detached checkout.
    """
    from ._command_state import CommandError, State, equal, present
    from ._doc_patch import apply_change, identities, managed_edges, validate_payload, value_at
    from ._recording import guard
    from .emitter import _diff, _outside, _payload_for_emit, emit_document_commands

    outside = [_outside(bundle) for bundle in (base, head, draft)]
    for value in outside:
        value.pop("placements", None)
    edges = [managed_edges("parent", {"occurrences": bundle["placements"]}) for bundle in (base, head, draft)]
    if not all(equal(outside[0], value) for value in outside[1:]) or not all(
            equal(edges[0], value) for value in edges[1:]):
        raise TimelineEditError("not saved: another writer or this draft changed closure membership or provenance; reopen it")
    if "source_mapping" in base:
        # Older local files retain native owner provenance without a canonical
        # Runtime image. Check those owners without inventing a physical closure.
        states = [State(bundle) for bundle in (base, head, draft)]
        for row in base["placements"]:
            owner = "occurrence:" + row["occurrence_id"] + "/shot"
            expected = present(states[0].entity(owner)["shot_id"])
            for state in states[1:]:
                guard(state, owner, ["shot_id"], expected)
        if "_runtime_document" in base["source_mapping"]:
            # Full images also use the emitter's physical ownership/alias checks.
            emit_document_commands(base, {**draft, "placements": base["placements"]})
            emit_document_commands(base, {**head, "placements": base["placements"]})

    merged, conflicts = copy.deepcopy(head), []
    roots = [("parent", base["parent"], head["parent"], draft["parent"], merged, "parent")]
    roots.append(("parent", {"occurrences": base["placements"]}, {"occurrences": head["placements"]},
                  {"occurrences": draft["placements"]}, merged, "placements"))
    for local, shot in base["shots"].items():
        for field, kind in (("payload", "shot:"), ("internal_timeline", "internal:")):
            roots.append((kind + local, shot[field], head["shots"][local][field],
                          draft["shots"][local][field], merged["shots"][local], field))
    for ref, old, theirs, ours, destination, field in roots:
        for payload in (old, theirs, ours):
            validate_payload(ref, payload)
        changes = []
        _diff(_payload_for_emit(ref, old), _payload_for_emit(ref, ours), ref, (), changes)
        if not changes:
            continue
        # Only bounded clip scalar helpers can merge independently within a
        # payload. Uncaptured edits and structural changes retain scope guards.
        paths = [change["path"] for change in changes]
        clip_paths = all(len(path) >= 3 and path[0] == "clips" and path[1].startswith("id:")
                         and path[2] in ("at", "from", "to", "hold", "params") for path in paths)
        scopes = list(dict.fromkeys(tuple(path[:2]) for path in paths)) if bounded and clip_paths else [()]
        if scopes != [()]:
            if identities(old["clips"], "id", ref=ref, path=["clips"]) != identities(
                    theirs.get("clips"), "id", ref=ref, path=["clips"]):
                raise TimelineEditError("not saved: another writer changed clip membership or order; reopen it")
            if ref != "parent" and not equal(base["parent"], head["parent"]):
                conflicts.append("parent/settings")
                if not force:
                    continue
            if ref.startswith("internal:"):
                rows = [[{key: value for key, value in row.items() if key != "duration_ms"}
                         for row in bundle["placements"] if row["shot_id"] == ref[len("internal:"):]]
                        for bundle in (base, head)]
                if not equal(*rows):
                    conflicts.append(ref + "/placement")
                    if not force:
                        continue
            settings = []
            for payload in (old, theirs):
                settings.append(copy.deepcopy(_payload_for_emit(ref, payload)))
                settings[-1].pop("clips", None)
            if not equal(*settings):
                conflicts.append(ref + "/settings")
                if not force:
                    continue
        landed = copy.deepcopy(theirs)
        for scope in scopes:
            before = value_at(old, ref, scope)
            current = value_at(theirs, ref, scope)
            desired = value_at(ours, ref, scope)
            if equal(current, desired):
                continue
            if not equal(before, current):
                conflicts.append("/".join((ref, *scope)))
                if not force:
                    continue
            # Emit against the exact landing operand. Even explicit local force
            # goes through the normal previous-value checks, at this scope only.
            pending = []
            if scope:
                _diff(current["value"], desired["value"], ref, scope, pending)
            else:
                _diff(_payload_for_emit(ref, theirs), _payload_for_emit(ref, ours), ref, (), pending)
            try:
                for change in pending:
                    landed, _ = apply_change(landed, change)
            except CommandError as exc:
                raise TimelineEditError(f"not saved: another writer's draft cannot replay {ref}: {exc}") from exc
        destination[field] = landed["occurrences"] if field == "placements" else landed
    return merged, conflicts


def _clip_changes(before_text: str, after_text: str) -> tuple[list[str], list[str]]:
    """The clip ids whose clip data differs between two saved texts (added, removed or edited), and their cut ids."""
    def clips_of(text: str) -> dict[str, Any]:
        return {c.id: c for c in Checkout(Checkout({})._absorb(json.loads(text))).clips()}

    old, new = clips_of(before_text), clips_of(after_text)
    ids: list[str] = []
    cuts: set[str] = set()
    for cid in sorted(set(old) | set(new)):
        a, b = old.get(cid), new.get(cid)
        if a is None or b is None or a.data != b.data:
            ids.append(cid)
            for clip in (a, b):
                if clip is not None and intent.cut_of(clip.data):
                    cuts.add(intent.cut_of(clip.data))
    return ids, sorted(cuts)


def _same_document(target: Path, previous: str, text: str) -> bool:
    """True when two working-copy texts hold the same document once the A1 migration is set aside.

    Both sides go through the same read as ``Checkout.load`` (hand ``_timeline`` edits applied, derived fields
    dropped, intent migrated) and then ``normalized``. A hand edit that changes content is still not the same."""
    try:
        before = _ns.normalized(Checkout({}, path=target)._absorb(json.loads(previous)))
        after = _ns.normalized(Checkout({}, path=target)._absorb(json.loads(text)))
    except Exception:  # noqa: BLE001 - unreadable text is compared as a change (the conservative answer)
        return False
    return before == after


def _joined_label(journal: list[tuple[str, str | None]]) -> str:
    """The undo step's label: the labels of the edits it covers, joined (at most 160 characters)."""
    text = "; ".join(label for label, _snapshot in journal)
    return text if len(text) <= 160 else text[:157] + "..."


# every edit is one undo step (the outermost call; what it calls inside is part of it)
def _capture_local_scalar(function):
    from functools import wraps

    @wraps(function)
    def captured(clip, *args, **kwargs):
        tl = clip._tl
        if tl._edit_depth:
            return function(clip, *args, **kwargs)
        before = _store._snapshot(tl)
        result = function(clip, *args, **kwargs)
        after = _store._snapshot(tl)
        if before != after:
            tl._local_scalar_edits.append((before, after))
        return result

    return captured


for _name in ("on", "until", "enter_at", "nudge", "hold_for", "set_duration", "extend", "end_at", "set", "set_track", "clear_asset",
              "swap_asset", "set_beats", "keyframe_at", "remove", "remove_layer", "keep", "set_look", "tag", "move_to_cut"):
    setattr(Clip, _name, _store._journaled(getattr(Clip, _name), "clip"))
for _name in ("nudge", "set"):
    setattr(Clip, _name, _capture_local_scalar(getattr(Clip, _name)))
for _name in ("add", "ripple_delete", "close_gap", "insert_time", "insert_line", "remove_line", "apply_script",
              "fill_slot", "fill_standins", "declare_gaps", "adopt", "add_cut", "set_cut_note"):
    setattr(Checkout, _name, _store._journaled(getattr(Checkout, _name), "checkout"))
Cut.split = _store._journaled(Cut.split, "cut")
StepSequence.fit = _store._journaled(StepSequence.fit, "cut")
for _name in ("replace", "set_gap_after"):
    setattr(Voice, _name, _store._journaled(getattr(Voice, _name), "voice"))
