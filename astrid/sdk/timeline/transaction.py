"""Atomic edit transactions over a detached timeline working copy."""
from __future__ import annotations

import contextlib
import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline import journal as _journal
from astrid.sdk.timeline import store as _store
from astrid.sdk.timeline.check import CheckReport, describe_changes
from astrid.sdk.timeline.doc import _strip_derived


@dataclass(frozen=True)
class Plan:
    label: str
    calls: tuple = ()
    changed: bool = False
    changes: tuple[str, ...] = ()
    diff: Mapping[str, Any] = field(default_factory=dict)
    clips: tuple[str, ...] = ()
    cuts: tuple[str, ...] = ()
    lint_delta: Mapping[str, Any] = field(default_factory=lambda: {"new": [], "resolved": []})
    blocking: tuple[str, ...] = ()
    sweep: tuple[str, ...] = ()
    digests: Mapping[str, str] = field(default_factory=dict)
    # Internal recorder output feeds CommitChanges; Plan's existing wire schema stays unchanged.
    direct_writes: tuple = ()
    consequences: tuple = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "calls": [call.to_json() if hasattr(call, "to_json") else call for call in self.calls],
            "changed": self.changed,
            "changes": list(self.changes),
            "diff": copy.deepcopy(dict(self.diff)),
            "clips": list(self.clips),
            "cuts": list(self.cuts),
            "lint_delta": {key: list(value) for key, value in self.lint_delta.items()},
            "blocking": list(self.blocking),
            "sweep": list(self.sweep),
            "digests": dict(self.digests),
        }


class Edit:
    """The transaction report yielded by ``with checkout.edit(...)``."""

    def __init__(self, label: str, *, dry_run: bool = False, reason: str | None = None):
        self.label = str(label)
        self.dry_run = bool(dry_run)
        self.reason = reason
        self.changed = False
        self.changes: list[str] = []
        self.plan: Plan | None = None
        self.report: CheckReport | None = None
        self.saved: Any = None
        self.calls: list[Any] = []
        self.failure: str | None = None
        self.canonical = False


def _digest(document: Any) -> str:
    return _journal.document_digest(document)


def _affected_cuts(before: Any, after: Any) -> list[str]:
    old = {clip.id: clip for clip in before.clips()}
    new = {clip.id: clip for clip in after.clips()}
    affected: set[str] = set()
    for clip_id in set(old) | set(new):
        left, right = old.get(clip_id), new.get(clip_id)
        if left is None or right is None or left.data != right.data or round(left.start * after.fps) != round(right.start * after.fps):
            for clip in (left, right):
                cut_id = intent.cut_of(clip.data) if clip is not None else None
                if cut_id:
                    affected.add(cut_id)
    order = [group["id"] for group in after._cut_groups()]
    return sorted(affected, key=lambda cut_id: order.index(cut_id) if cut_id in order else len(order))


def _record(tl: Any, label: str, outcome: str, *, reason: str | None, before: str | None = None,
            after: str | None = None, clips=(), cuts=(), error: str | None = None, calls=()) -> None:
    if tl.path:
        _journal.record_or_note(tl, Path(tl.path), kind="edit", outcome=outcome, label=label,
                                before=before, after=after, clips=tuple(clips), cuts=tuple(cuts),
                                reason=reason, error=error, calls=tuple(calls))


def _restore_value(current, previous):
    # Restore live view dictionaries in place where possible, so a retained Clip
    # cannot continue exposing a partially written value after a refusal.
    if isinstance(current, dict) and isinstance(previous, dict):
        for key in list(current):
            if key not in previous:
                del current[key]
        for key, value in previous.items():
            current[key] = _restore_value(current.get(key), value)
        return current
    if isinstance(current, list) and isinstance(previous, list):
        by_id = {row["id"]: row for row in current if isinstance(row, dict) and "id" in row}
        restored = []
        for index, value in enumerate(previous):
            old = by_id.get(value.get("id")) if isinstance(value, dict) and "id" in value else current[index] if index < len(current) else None
            restored.append(_restore_value(old, value))
        current[:] = restored
        return current
    return copy.deepcopy(previous)


def _restore(tl: Any, bundle: Any, journal_len: int, notes_len: int) -> None:
    tl.bundle = _restore_value(tl.bundle, bundle)
    tl._refresh()
    del tl._journal[journal_len:]
    del tl.notes[notes_len:]


def _plan(label: str, *, changed: bool, changes=(), diff=None, clips=(), cuts=(), digests=None,
          report: CheckReport | None = None, calls=(), direct_writes=(), consequences=()) -> Plan:
    return Plan(label=label, calls=tuple(calls), direct_writes=tuple(direct_writes), consequences=tuple(consequences), changed=changed, changes=tuple(changes), diff=diff or {}, clips=tuple(clips),
                cuts=tuple(cuts), lint_delta={"new": list((report.problems or []) if report else []), "resolved": []},
                blocking=tuple(report.blocking) if report else (), sweep=tuple(cuts), digests=dict(digests or {}))


@contextlib.contextmanager
def edit_transaction(tl: Any, label: str, *, dry_run: bool = False, check: bool = True,
                     quiet: bool = False, reason: str | None = None, persist: bool = True) -> Iterator[Edit]:
    if getattr(tl, "_edit_depth", 0):
        yield tl._open_edit
        return

    tx = Edit(label, dry_run=dry_run, reason=reason)
    entry = copy.deepcopy(tl.bundle)
    journal_len, notes_len = len(tl._journal), len(tl.notes)
    before_snapshot = _store._snapshot(tl)
    tl._edit_depth, tl._open_edit = 1, tx
    try:
        from astrid.core.timeline.compatibility import require_writable_candidate, TimelineCompatibilityError
        from astrid.sdk.timeline.commands import CommandError, direct_writes, guard_direct_writes
        try:
            require_writable_candidate(tl.document())
        except TimelineCompatibilityError as exc:
            raise CommandError(str(exc)) from None
        yield tx
        guards = direct_writes(tl, tx.calls)
        if _store._snapshot(tl) != before_snapshot:
            # Resolve through today's timing path. Internal compiler writes join this
            # transaction and do not recursively produce another command.
            tl._command_depth = getattr(tl, "_command_depth", 0) + 1
            try:
                tl.resolve()
            finally:
                tl._command_depth -= 1
        guard_direct_writes(tl, guards)
        from astrid.sdk.timeline.commands import compact_calls
        tx.calls[:] = compact_calls(tx.calls, tl)
        before_doc = _strip_derived(copy.deepcopy(entry))
        after_doc = _strip_derived(tl.document())
        digests = {"before": _digest(before_doc), "after": _digest(after_doc)}
        if _store._snapshot(tl) == before_snapshot:
            from astrid.sdk.timeline.commands import finish_recording
            finish_recording(tl, tx, entry)
            tx.plan = _plan(label, changed=False, digests=digests, calls=tx.calls,
                            direct_writes=getattr(tx, "direct_writes", ()), consequences=getattr(tx, "consequences", ()))
            if persist and not dry_run:
                _record(tl, label, "no-change", reason=reason, before=digests["before"], after=digests["after"], calls=tx.calls)
            if not quiet:
                print(f"no change: {label}")
            return

        from astrid.sdk.timeline_cuts import diff_bundles
        previous = tl.__class__(copy.deepcopy(entry))
        changes = describe_changes(previous, tl)
        diff = diff_bundles(before_doc, after_doc)
        clips = list(dict.fromkeys(str(row["clip_id"]) for row in diff.get("changes", []) if row.get("clip_id") is not None))
        cuts = _affected_cuts(previous, tl)
        tx.changed, tx.changes = True, changes
        # Quick checks happen before persistence. Existing compatibility guards
        # remain in the save path; pre-existing film lint is not a new refusal.
        local_only = getattr(tl, "_edit_local_only", False)
        tl._edit_local_only = True
        tl._command_depth = getattr(tl, "_command_depth", 0) + 1
        try:
            tx.report = tl.check() if check else None
        finally:
            tl._edit_local_only = local_only
            tl._command_depth -= 1
        guard_direct_writes(tl, guards)
        from astrid.sdk.timeline.commands import finish_recording
        finish_recording(tl, tx, entry)
        tx.plan = _plan(label, changed=True, changes=changes, diff=diff, clips=clips, cuts=cuts,
                        digests=digests, report=tx.report, calls=tx.calls,
                        direct_writes=getattr(tx, "direct_writes", ()), consequences=getattr(tx, "consequences", ()))
        if dry_run:
            _restore(tl, entry, journal_len, notes_len)
            # Explicit legacy SDK audit transactions retain their audit record.
            # Executable command previews (canonical=True) write nothing.
            if not tx.canonical:
                _record(tl, label, "dry-run", reason=reason, before=digests["before"], after=digests["after"],
                        clips=clips, cuts=cuts, calls=tx.calls)
            if not quiet:
                print("dry run (nothing saved):")
                for line in changes:
                    print(f"  {line}")
            return

        tl._journal.append((str(label), before_snapshot))
        if persist and tl.path:
            tx.saved = tl.save(quiet=True)
        if not quiet:
            where = "saved" if tx.saved else "in memory"
            first = tx.report.brief() if tx.report is not None else ""
            print(f'edit "{label}": {len(changes)} change(s) · {where}' + (f" · {first}" if first else ""))
    except BaseException as exc:
        _restore(tl, entry, journal_len, notes_len)
        if not dry_run:
            from astrid.sdk.timeline.commands import call_label, compact_calls
            tx.calls[:] = compact_calls(tx.calls)
            record_label = call_label(tx.calls) if tx.canonical and tx.calls else label
            _record(tl, record_label, "rolled-back", reason=reason, error=tx.failure or _journal.one_line(exc), calls=tx.calls)
        raise
    finally:
        tl._edit_depth, tl._open_edit = 0, None
