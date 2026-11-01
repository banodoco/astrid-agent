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
            after: str | None = None, clips=(), cuts=(), error: str | None = None) -> None:
    if tl.path:
        _journal.record_or_note(tl, Path(tl.path), kind="edit", outcome=outcome, label=label,
                                before=before, after=after, clips=tuple(clips), cuts=tuple(cuts),
                                reason=reason, error=error)


def _restore(tl: Any, bundle: Any, journal_len: int, notes_len: int) -> None:
    tl.bundle = bundle
    tl._refresh()
    del tl._journal[journal_len:]
    del tl.notes[notes_len:]


def _plan(label: str, *, changed: bool, changes=(), diff=None, clips=(), cuts=(), digests=None,
          report: CheckReport | None = None) -> Plan:
    return Plan(label=label, changed=changed, changes=tuple(changes), diff=diff or {}, clips=tuple(clips),
                cuts=tuple(cuts), lint_delta={"new": list((report.problems or []) if report else []), "resolved": []},
                blocking=tuple(report.blocking) if report else (), sweep=tuple(cuts), digests=dict(digests or {}))


@contextlib.contextmanager
def edit_transaction(tl: Any, label: str, *, dry_run: bool = False, check: bool = True,
                     quiet: bool = False, reason: str | None = None) -> Iterator[Edit]:
    if getattr(tl, "_edit_depth", 0):
        outer = getattr(tl, "_open_edit", None)
        yield outer if outer is not None else Edit(label, dry_run=dry_run, reason=reason)
        return

    tx = Edit(label, dry_run=dry_run, reason=reason)
    entry = copy.deepcopy(tl.bundle)
    journal_len, notes_len = len(tl._journal), len(tl.notes)
    before_snapshot = _store._snapshot(tl)
    tl._edit_depth = 1
    tl._open_edit = tx
    try:
        yield tx
    except BaseException as exc:
        _restore(tl, entry, journal_len, notes_len)
        _record(tl, label, "rolled-back", reason=reason, error=_journal.one_line(exc))
        raise
    finally:
        tl._edit_depth = 0
        tl._open_edit = None

    before_doc = _strip_derived(copy.deepcopy(entry))
    after_doc = _strip_derived(tl.document())
    digests = {"before": _digest(before_doc), "after": _digest(after_doc)}
    if _store._snapshot(tl) == before_snapshot:
        tx.plan = _plan(label, changed=False, digests=digests)
        _record(tl, label, "no-change", reason=reason, before=digests["before"], after=digests["after"])
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

    if dry_run:
        try:
            report = tl.check() if check else None
        finally:
            _restore(tl, entry, journal_len, notes_len)
        tx.report = report
        tx.plan = _plan(label, changed=True, changes=changes, diff=diff, clips=clips, cuts=cuts,
                        digests=digests, report=report)
        _record(tl, label, "dry-run", reason=reason, before=digests["before"], after=digests["after"],
                clips=clips, cuts=cuts)
        if not quiet:
            print("dry run (nothing saved):")
            for line in changes:
                print(f"  {line}")
        return

    tl._journal.append((str(label), before_snapshot))
    try:
        if tl.path:
            tx.saved = tl.save(quiet=True)
    except BaseException as exc:
        _restore(tl, entry, journal_len, notes_len)
        _record(tl, label, "rolled-back", reason=reason, error=_journal.one_line(exc))
        raise
    tx.report = tl.check() if check else None
    tx.plan = _plan(label, changed=True, changes=changes, diff=diff, clips=clips, cuts=cuts,
                    digests=digests, report=tx.report)
    if not quiet:
        where = "saved" if tx.saved else "in memory"
        first = tx.report.brief() if tx.report is not None else ""
        print(f'edit "{label}": {len(changes)} change(s) · {where}' + (f" · {first}" if first else ""))

