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
from astrid.sdk.timeline.identity import retain_cut_reservations
from astrid.sdk.timeline.lookup import _LookupMixin
from astrid.sdk.timeline.model import (Clip, Cut, StepSequence, Voice)
from astrid.sdk.timeline.publish import _PublishMixin
from astrid.sdk.timeline.reflow import _ReflowMixin
from astrid.sdk.timeline.transaction import edit_transaction


# ------------------------------------------------------------------ checkout

class Checkout(_LookupMixin, _EditMixin, _ReflowMixin, _CheckMixin, _PublishMixin, _Suggest):
    """A complete, editable copy of one timeline head (parent → shots → internal timelines)."""

    def __init__(self, bundle: dict[str, Any], *, path: Path | None = None):
        intent.migrate(bundle)  # a working copy holds Astrid intent under app.astrid only (old places move in; reads stay dual)
        self.bundle = bundle
        self.path = path
        self.fps = bundle_fps(bundle) or 30.0
        self.notes: list[str] = []
        self.report: list[str] = []  # what the last re-flow did, in plain words
        self._mcache: dict[str, Any] | None = None
        # Re-flow keeps the music's ending: the bed is cut at a bar with a crossfade so it ends on the film's
        # end ("keep_end"). "plays_on" keeps the bed whole, as it was before (the picture moves against it).
        self.music = "keep_end"
        self._keep_inside = False  # a removal keeps the clips inside the window (as orphans) instead of deleting them
        self._kept: list[str] = []
        self._outside: list[str] = []
        self._journal: list[tuple[str, str | None]] = []  # (label, the document before that edit), one per edit
        self._pending_calls: list[Any] = []
        self._edit_depth = 0
        self._open_edit: Any = None  # the Edit of the tl.edit now open (nested edits join it)
        self._orphan_reason = "its line was removed"
        self._disk: str | None = None  # the file text this handle loaded (or last saved): what another writer may change
        self.merged: list[str] = []      # what the last save merged in from another writer, in plain words

    def _refresh(self) -> None:
        """Rebuild derived checkout state after an edit transaction restores its bundle."""
        self.fps = bundle_fps(self.bundle) or 30.0
        self._mcache = None

    # ---- open / save ------------------------------------------------------
    @classmethod
    def open(cls, project: str, timeline: str, *, revision_id: str | None = None, client: Any = None) -> "Checkout":
        """Check out the current head (read-only until ``publish``)."""
        bundle = _store.fetch_bundle(project, timeline, revision_id=revision_id, client=client)
        intent.migrate(bundle)  # the fetched copy is ours; the published revision is never rewritten
        return cls(bundle)

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
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text)
        tl = cls({}, path=path)
        tl.bundle = tl._absorb(raw)
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
        with _store._file_lock(target):
            saved = self._save_locked(target, force=force)
        if (self.SAY_SAVED if quiet is None else not quiet) and not _store._cli_running():
            name = getattr(self, "draft_name", None)
            changed = len(self.edits().get("changes") or [])
            print(f"saved {'working copy ' + repr(name) if name else saved} · {changed} clip(s) changed vs published"
                  + (f" · {self.merged[0]}" if self.merged else "") + " · tl.check().brief() says what blocks publishing")
        return saved

    def _save_locked(self, target: Path, *, force: bool, record: bool = True) -> Path:
        self.merged = []
        if self._disk is not None and self.path and Path(self.path) == target and target.is_file():
            now = target.read_text(encoding="utf-8")
            if now != self._disk:
                self._merge_writer(target, now, force=force)
        self.notes.extend(self._sync_film_length())  # the film's length follows its content, before it is written
        text = _serialize(self._annotated())
        staged: list[tuple[Path, Path]] = []  # undo steps: written under a temp name now, committed after the file
        clear_redo = False
        journaled_step: tuple[str, str, str] | None = None  # (label, previous text, step file name) of the step staged now
        if record and target.is_file():
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
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)  # a reader never sees half a file
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
        pending_calls = list(self._pending_calls)
        if self._open_edit is not None:
            pending_calls.extend(self._open_edit.calls)
        self._journal = []
        self._pending_calls = []
        self.path = target
        self._disk = text
        if journaled_step is not None:  # the journal: one record per undo step (the file and the step are committed now)
            label, before_text, undo_step = journaled_step
            from astrid.sdk.timeline.commands import compact_calls, call_label
            pending_calls = compact_calls(pending_calls, self)
            if pending_calls and (self._open_edit is None or self._open_edit.canonical):
                label = call_label(pending_calls)
            try:
                clips, cuts = _clip_changes(before_text, text)
                _journal.record_working_copy(target, owner=self, kind="save", outcome="changed", label=label,
                                             before=_journal.digest(before_text), after=_journal.digest(text),
                                             clips=tuple(clips), cuts=tuple(cuts), undo_step=undo_step, calls=tuple(pending_calls))
            except Exception as exc:  # noqa: BLE001 - the save is committed; only its journal record is missing
                _journal.note_failure(self, exc)
        return target

    def _merge_writer(self, target: Path, now: str, *, force: bool) -> None:
        """Another writer saved this file since we loaded it: put our edit on top of theirs."""
        import datetime as _dt

        mine = Checkout({}, path=target)
        base = mine._absorb(json.loads(self._disk or "{}"))
        theirs = mine._absorb(json.loads(now))
        when = _dt.datetime.fromtimestamp(target.stat().st_mtime).strftime("%H:%M:%S")
        merged, conflicts = _store.three_way(base, theirs, self.bundle, force=force, whole=True)
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

    def edit_flags(self, flags: Mapping[str, Any], *, client: Any = None) -> None:
        """Execute the existing CLI edit adapter through the shared runner."""
        import argparse
        from astrid.packs.timeline.cli import _configure_edit, _apply_edit
        from astrid.sdk.timeline.commands import EDIT_FLAG_OPERATIONS, CommandError
        unknown = set(flags) - set(EDIT_FLAG_OPERATIONS)
        if unknown:
            raise CommandError(f"unknown edit flags: {', '.join(sorted(unknown))}")
        parser = argparse.ArgumentParser(add_help=False)
        _configure_edit(parser)
        parsed = parser.parse_args([])
        vars(parsed).update(flags)
        parsed.client = client
        if parsed.music is not None:
            from astrid.packs.timeline.cli_music import apply_music_policy
            apply_music_policy(self, parsed)
        # This adapter expands to the same individual operations as the SDK.
        depth, self._command_depth = self._command_depth, 0
        try:
            _apply_edit(self, parsed)
        finally:
            self._command_depth = depth

    def batch(self, commands: list[Mapping[str, Any]]) -> list[Any]:
        """Run ordered OpCalls as one atomic edit and one undo step."""
        from astrid.sdk.timeline.commands import execute, CommandError
        if not isinstance(commands, list) or not commands:
            raise CommandError("batch requires a non-empty list of EditCommands")
        return [execute(self, command) for command in commands]

    def undo(self, steps: int = 1, *, client=None, publication: str | None = None) -> list[str]:
        """Go back ``steps`` edits of this working copy (``redo`` goes forward). Returns what was undone."""
        if not self.path:
            raise TimelineEditError("only a saved working copy can undo")
        from astrid.sdk.timeline.saved_intent import read_saved_calls
        from astrid.sdk.timeline.inverse import publications, stage
        # Publication undo precedes the old snapshot stack, which can contain
        # pre-publication edits. Never silently resurrect that obsolete base.
        known = publications(self.bundle["project_id"], self.bundle["timeline_id"])
        own = any(value["draft"] == Path(self.path).stem for value in known.values())
        if publication is not None or (own and not read_saved_calls(self)):
            if steps != 1:
                raise TimelineEditError("publication undo accepts one named publication at a time")
            if client is None:
                from astrid.sdk import AstridClient
                with AstridClient.open_from_launcher(start_pack_host=False) as opened:
                    return stage(self, client=opened, publication=publication)
            return stage(self, client=client, publication=publication)
        target = Path(self.path)
        history = sorted(_store._history_dir(target).glob("*.json"))
        if not history:
            raise TimelineEditError("nothing to undo: this working copy has no earlier edit")
        undone = []
        start = current = target.read_text(encoding="utf-8")
        for step in reversed(history[-max(1, min(int(steps), len(history))):]):
            label, state = _store._read_step(step)
            _store._write_step(_store._redo_dir(target), label, current)
            current = state
            step.unlink()
            undone.append(label)
        current = _serialize(retain_cut_reservations(json.loads(current), json.loads(start)))
        target.write_text(current, encoding="utf-8")
        self._reload()
        _journal.record_or_note(self, target, kind="undo", outcome="changed", label="; ".join(undone)[:160],
                                     before=_journal.digest(start), after=_journal.digest(current),
                                     calls=(self._storage_call,) if getattr(self, "_storage_call", None) else ())
        return undone

    def redo(self, steps: int = 1) -> list[str]:
        """Re-apply edits that ``undo`` took back. Returns what was redone."""
        if not self.path:
            raise TimelineEditError("only a saved working copy can redo")
        target = Path(self.path)
        stack = sorted(_store._redo_dir(target).glob("*.json"))
        if not stack:
            raise TimelineEditError("nothing to redo")
        redone = []
        start = current = target.read_text(encoding="utf-8")
        for step in reversed(stack[-max(1, min(int(steps), len(stack))):]):
            label, state = _store._read_step(step)
            _store._write_step(_store._history_dir(target), label, current)
            current = state
            step.unlink()
            redone.append(label)
        current = _serialize(retain_cut_reservations(json.loads(current), json.loads(start)))
        target.write_text(current, encoding="utf-8")
        self._reload()
        _journal.record_or_note(self, target, kind="redo", outcome="changed", label="; ".join(redone)[:160],
                                     before=_journal.digest(start), after=_journal.digest(current),
                                     calls=(self._storage_call,) if getattr(self, "_storage_call", None) else ())
        return redone

    def _reload(self) -> None:
        fresh = Checkout.load(self.path)
        self.bundle, self._mcache, self._journal, self._disk = fresh.bundle, None, [], fresh._disk

    @contextlib.contextmanager
    def step(self, label: str):
        """One undo step for everything inside (a verb that makes several changes is one edit)."""
        outer = not self._edit_depth
        with edit_transaction(self, label, check=False, quiet=True, persist=False) as tx:
            if outer:
                tx.canonical = True
            yield self
        if outer and tx.changed:
            self._pending_calls.extend(tx.calls)

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
        else:
            fetched = _store.fetch_bundle(project, timeline, client=client)
            intent.migrate(fetched)  # checkout: legacy intent -> app.astrid in the working copy (its pinned base copies are migrated too, so the move is not an edit; the published revision is untouched)
            cls._pin_initial_base(fetched)
            tl = cls(fetched)
            path.parent.mkdir(parents=True, exist_ok=True)
            tl.save(path)
            _journal.record_or_note(tl, path, kind="checkout", outcome="changed", label="checkout",
                                         after=_journal.digest(path.read_text(encoding="utf-8")),
                                         evidence=({"kind": "saved-base-v1", "document": tl.document()},))
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
        source = _store.fetch_bundle(project, timeline, revision_id=revision_id, client=client)
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


# Every public document mutator enters the command runner. Lifecycle and I/O
# (save, undo, redo, publish, draft/discard) keep their existing storage boundaries.
from astrid.sdk.timeline import commands as _commands

_commands.register(Clip, "clip", (
    "on", "until", "enter_at", "nudge", "hold_for", "for_", "set_duration", "trim", "slip", "extend", "end_at",
    "set", "set_param", "clear", "set_track", "clear_asset", "swap_asset", "set_beats", "keyframe_at",
    "remove", "remove_layer", "keep", "set_look", "tag", "move_to_cut"))
_commands.register(Checkout, "timeline", (
    "add", "ripple_delete", "close_gap", "insert_time", "insert_line", "remove_line", "apply_script",
    "fill_slot", "fill_standins", "declare_gaps", "adopt", "add_cut", "set_cut_note", "replace_asset",
    "reflow", "retime", "resolve", "film_end", "batch", "edit_flags"))
_commands.register(Cut, "cut", ("split", "set_note", "shorten"))
_commands.register(StepSequence, "sequence", ("fit",))
_commands.register(Voice, "voice", ("replace", "set_gap_after"))

_commands.register(Checkout, "timeline", ("save", "undo", "redo"), boundary="storage")

from astrid.sdk.timeline.inverse import apply_inverse as _apply_inverse
_commands.REGISTRY["timeline.inverse"] = _commands.Operation("timeline.inverse", "timeline", "inverse", _apply_inverse)
