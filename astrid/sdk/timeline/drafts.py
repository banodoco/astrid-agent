"""Conservative, timeline-scoped maintenance of disposable working copies.

The shared append-only timeline audit is history, not a disposable draft sidecar.
Unknown bases and unreadable drafts are retained just like unpublished edits.
"""
from __future__ import annotations

import contextlib
import math
import os
import shutil
import time
import uuid
from pathlib import Path

from astrid.sdk.timeline import history, store
from astrid.sdk.timeline._common import TimelineEditError

EXPIRY_DAYS = 30.0
EXPIRY_ENV = "ASTRID_DRAFT_EXPIRY_DAYS"


def _leased(path: Path) -> bool:
    return path.with_suffix(".lease").exists() or any(path.with_suffix(".leases").glob("*"))


@contextlib.contextmanager
def draft_lease(path: Path):
    """Keep a draft across automatic maintenance until this explicit lease ends.

    A crashed owner's lease stays in place, conservatively requiring release.
    Each owner removes only its own marker; overlapping leases cannot cancel.
    """
    path = Path(path)
    marker = path.with_suffix(".leases") / uuid.uuid4().hex
    with store._file_lock(path):
        if not path.is_file():
            raise TimelineEditError("cannot lease an absent working copy")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(os.getpid()), encoding="utf-8")
    try:
        yield path
    finally:
        with store._file_lock(path):
            marker.unlink(missing_ok=True)
            if path.is_file():
                path.touch()  # releasing a lease starts a fresh idle interval


def remove_draft(path: Path) -> None:
    """Remove only this draft and its sidecars, under its checkout lock."""
    path.unlink(missing_ok=True)
    for folder in (store._history_dir(path), store._redo_dir(path), path.with_suffix(".leases")):
        shutil.rmtree(folder, ignore_errors=True)
    for marker in (history.pending_path(path), path.with_suffix(".lease"),
                   path.with_suffix(path.suffix + ".journal.jsonl")):
        marker.unlink(missing_ok=True)
    for pattern in (f".{path.name}.*.tmp", f".{history.pending_path(path).name}.*.tmp"):
        for temporary in path.parent.glob(pattern):
            temporary.unlink(missing_ok=True)
    pointer = path.parent / ".current"
    if pointer.is_file() and pointer.read_text(encoding="utf-8").strip() == path.stem:
        pointer.unlink(missing_ok=True)
    # Keep the stable lock inode: removing it could split queued writers into
    # separate locks. It contains coordination only, never draft content.


def expire_drafts(project_id: str, timeline_id: str, *, days: float | None = None,
                  now: float | None = None) -> list[str]:
    """Expire only provably clean, idle, unleased drafts of this one timeline."""
    from astrid.sdk.timeline.checkout import Checkout

    try:
        days = float(os.environ.get(EXPIRY_ENV, EXPIRY_DAYS) if days is None else days)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(days) or days < 0:
        return []
    cutoff = (time.time() if now is None else now) - days * 86400
    removed = []
    folder = store.draft_path(project_id, timeline_id).parent
    for path in sorted(folder.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            if path.is_symlink() or _leased(path) or path.stat().st_mtime > cutoff:
                continue
            with store._file_lock(path, wait_seconds=0):
                if not path.is_file() or _leased(path) or path.stat().st_mtime > cutoff:
                    continue
                if history.pending_path(path).is_file():
                    continue  # unresolved undo/redo is work, never expiry fodder
                known, changes = store.checkout_change_units(Checkout.load(path), complete=True)
                if known and not changes:
                    remove_draft(path)
                    removed.append(path.stem)
        except (OSError, ValueError, TypeError, KeyError, TimelineEditError):
            continue  # contention or unreadable/unverifiable provenance: retain
    return removed


def list_drafts(project_id: str, timeline_id: str) -> list[dict]:
    from astrid.sdk.timeline.checkout import Checkout

    rows = []
    for path in sorted(store.draft_path(project_id, timeline_id).parent.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            known, changes = store.checkout_change_units(Checkout.load(path), complete=True)
        except (OSError, ValueError, TypeError, KeyError, TimelineEditError):
            known, changes = False, []
        rows.append({"draft": path.stem, "change_state": "unknown" if not known else "changed" if changes else "clean",
                     "changed": bool(changes) if known else None, "leased": _leased(path)})
    return rows
