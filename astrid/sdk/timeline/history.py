"""Recoverable local undo/redo transitions, serialized by the checkout lock.

A write-ahead record owns the exact destination names and states. Replaying it
is idempotent whether the document write or a source removal was interrupted.
This is a local storage protocol; commands retain their existing vocabulary.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from astrid.sdk.timeline import journal, store
from astrid.sdk.timeline.doc import _serialize
from astrid.sdk.timeline.identity import retain_cut_reservations


def pending_path(target: Path) -> Path:
    return target.with_name(f".{target.name}.replay.json")


def _sync_directory(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    _sync_directory(target.parent)


def recover(target: Path) -> bool:
    """Finish a pending transition under the target lock; never allocate new steps."""
    pending = pending_path(target)
    if not pending.is_file():
        return False
    transaction = json.loads(pending.read_text(encoding="utf-8"))
    redo = transaction["kind"] == "redo"
    source = store._redo_dir(target) if redo else store._history_dir(target)
    destination = store._history_dir(target) if redo else store._redo_dir(target)
    # Validate names before using persisted data as paths.
    for step in transaction["steps"]:
        for key in ("source", "destination"):
            name = step[key]
            if not isinstance(name, str) or Path(name).name != name or not name.endswith(".json"):
                raise ValueError("invalid history recovery step name")
    for step in transaction["steps"]:
        atomic_write(destination / step["destination"], json.dumps({"label": step["label"], "state": step["state"]}))
    atomic_write(target, transaction["after"])
    for step in transaction["steps"]:
        (source / step["source"]).unlink(missing_ok=True)
    _sync_directory(source)
    audit = transaction["audit"]
    journal_path = (journal.journal_path(audit["project"], audit["timeline"])
                    if store._is_draft(target) else target.with_suffix(target.suffix + ".journal.jsonl"))
    if not any(row.id == audit["id"] for row in journal.read(journal_path)):
        journal.append(journal_path, **audit)
    with journal_path.open("rb") as handle:
        os.fsync(handle.fileno())
    _sync_directory(journal_path.parent)
    pending.unlink()
    _sync_directory(target.parent)
    return True


def replay(target: Path, steps: list[Path], current: str, *, redo: bool, owner: Any) -> tuple[list[str], str]:
    destination = store._history_dir(target) if redo else store._redo_dir(target)
    sequence = max((int(p.name.split("-", 1)[0]) for p in destination.glob("*.json")
                    if p.name.split("-", 1)[0].isdigit()), default=0)
    before = current
    entries: list[dict[str, Any]] = []
    for step in steps:
        label, state = store._read_step(step)
        sequence += 1
        name = f"{sequence:020d}-{journal.document_digest(current)[7:23]}.json"
        entries.append({"source": step.name, "destination": name, "label": label, "state": current})
        current = _serialize(retain_cut_reservations(json.loads(state), json.loads(current)))
    kind = "redo" if redo else "undo"
    label = "; ".join(entry["label"] for entry in entries)[:160]
    audit = {"id": uuid.uuid4().hex, "project": str(owner.bundle.get("project_id") or "local"),
             "timeline": str(owner.bundle.get("timeline_id") or target.stem), "draft": target.stem,
             "actor": journal.current_actor(), "source": journal.current_source(label),
             "kind": kind, "outcome": "changed", "label": label,
             "before": journal.digest(before), "after": journal.digest(current),
             "calls": [owner._storage_call.to_json()] if getattr(owner, "_storage_call", None) else []}
    atomic_write(pending_path(target), json.dumps({"kind": kind, "steps": entries, "after": current, "audit": audit}))
    recover(target)
    return [entry["label"] for entry in entries], current
