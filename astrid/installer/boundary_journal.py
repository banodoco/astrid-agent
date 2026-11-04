"""Standalone, fail-closed boundary checkpoint journal; see docs/boundary-journal.md."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import uuid


class JournalError(ValueError):
    """The journal is invalid, conflicted, unsupported, or cannot be committed."""


_STEPS = ("stop", "adopt_receipt", "selection", "registration")
_NAME = re.compile(r"[0-9]{4}\.json\Z")
_MAX_BYTES = 65536


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JournalError("duplicate JSON key")
        result[key] = value
    return result


def _identity(operation_id, r0_root, r1_root, realm):
    try:
        if not isinstance(operation_id, str) or str(uuid.UUID(operation_id, version=4)) != operation_id:
            raise ValueError
    except (ValueError, AttributeError):
        raise JournalError("operation ID must be a canonical UUID4") from None
    roots = []
    for root in (r0_root, r1_root):
        if not isinstance(root, str) or not root or '\x00' in root:
            raise JournalError("invalid root")
        if not os.path.isabs(root) or os.path.realpath(root) != root:
            raise JournalError("roots must be canonical absolute paths")
        roots.append(root)
    try:
        common = os.path.commonpath(roots)
    except ValueError:
        common = None
    if common in roots:
        raise JournalError("roots overlap")
    if not isinstance(realm, str) or not realm or len(realm) > 1024 or '\x00' in realm:
        raise JournalError("invalid realm")
    return {"operation_id": operation_id, "r0_root": r0_root, "r1_root": r1_root, "realm": realm}


def _validate_events(events):
    if not isinstance(events, list) or len(events) > 10:
        raise JournalError("invalid checkpoints")
    next_step = 0
    pending = None
    halted = False
    rolled_back = False
    for event in events:
        if not isinstance(event, dict) or set(event) not in ({"step", "kind"}, {"step", "kind", "outcome"}):
            raise JournalError("invalid checkpoint")
        step, kind = event.get("step"), event.get("kind")
        if not isinstance(step, str) or not isinstance(kind, str):
            raise JournalError("invalid checkpoint")
        if kind == "intent" and set(event) == {"step", "kind"}:
            if rolled_back or (step != "rollback" and (pending is not None or halted or next_step >= len(_STEPS) or step != _STEPS[next_step])):
                raise JournalError("out of order intent")
            if step == "rollback":
                rolled_back = True
            pending = step
        elif kind == "effect" and set(event) == {"step", "kind", "outcome"}:
            outcome = event["outcome"]
            if step != pending or outcome not in ("succeeded", "failed", "uncertain"):
                raise JournalError("invalid effect")
            # No Runtime receipt contract is available to validate a successful adoption.
            if step == "adopt_receipt" and outcome == "succeeded":
                raise JournalError("adopt receipt contract unavailable")
            pending = None
            if step != "rollback":
                if outcome == "succeeded":
                    next_step += 1
                else:
                    halted = True
        else:
            raise JournalError("invalid checkpoint")


def _check_placement(directory, identity):
    # The directory and its parents must be trusted and stable for the lifetime
    # of the operation. Canonicalization also catches existing symlink aliases.
    directory = os.path.abspath(os.fspath(directory))
    resolved = os.path.realpath(directory)
    for root in (identity["r0_root"], identity["r1_root"]):
        try:
            common = os.path.commonpath((resolved, root))
        except ValueError:
            common = None
        if common in (root, resolved):
            raise JournalError("journal must not overlap either data root")
    if resolved != directory or not os.path.isdir(directory):
        raise JournalError("journal directory must be an existing canonical directory")
    return Path(directory)


def _sync_dir(directory):
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read(path):
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise JournalError("invalid journal file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES
                or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)):
            raise JournalError("invalid journal file")
        with os.fdopen(fd, "rb", closefd=False) as f:
            raw = f.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise JournalError("journal file too large")
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(JournalError("invalid JSON constant")))
    finally:
        os.close(fd)


class BoundaryJournal:
    """One operation in an existing, dedicated, trusted journal directory."""

    def __init__(self, directory, *, operation_id, r0_root, r1_root, realm):
        if sys.platform != "linux":
            raise JournalError("journal durability is supported only on Linux")
        self.identity = _identity(operation_id, r0_root, r1_root, realm)
        self.directory = _check_placement(directory, self.identity)

    def _load(self, *, allow_empty=False):
        try:
            entries = sorted(os.listdir(self.directory))
            if not entries and allow_empty:
                return None
            if not entries or len(entries) > 11 or entries != [f"{i:04d}.json" for i in range(len(entries))]:
                raise JournalError("missing, foreign, or interrupted journal files")
            previous = None
            for index, name in enumerate(entries):
                if not _NAME.fullmatch(name):
                    raise JournalError("invalid journal filename")
                record = _read(self.directory / name)
                if not isinstance(record, dict) or set(record) != {"version", "identity", "events"} or type(record["version"]) is not int or record["version"] != 1 or record["identity"] != self.identity:
                    raise JournalError("foreign or invalid journal record")
                events = record["events"]
                _validate_events(events)
                if len(events) != index or (previous is not None and events[:-1] != previous):
                    raise JournalError("nonmonotonic journal")
                previous = events
            # A previous writer may have linked its file before a directory
            # fsync failed. Do not use it unless durability can now be obtained.
            _sync_dir(self.directory)
            return previous
        except JournalError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise JournalError("cannot reload journal") from exc

    def load(self):
        """Return checkpoints and the effective status of each recorded intent."""
        events = self._load()
        status = {}
        for event in events:
            status[event["step"]] = "uncertain" if event["kind"] == "intent" else event["outcome"]
        return {"identity": dict(self.identity), "events": events, "status": status}

    def _commit(self, events):
        _validate_events(events)
        record = {"version": 1, "identity": self.identity, "events": events}
        data = (json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
        if len(data) > _MAX_BYTES:
            raise JournalError("journal record too large")
        temp = None
        try:
            fd, temp = tempfile.mkstemp(prefix=".pending-", dir=self.directory)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            # Hard-link gives atomic create-if-absent; never replace another writer.
            os.link(temp, self.directory / f"{len(events):04d}.json")
            os.unlink(temp)
            temp = None
            _sync_dir(self.directory)
        except OSError as exc:
            # A failure after link has an ambiguous commit. Leave any temporary
            # file in place so reload refuses rather than hiding interruption.
            raise JournalError("checkpoint commit failed; inspect journal before recovery") from exc

    def create(self):
        if self._load(allow_empty=True) is not None:
            raise JournalError("journal already exists")
        self._commit([])

    def intent(self, step):
        events = self._load()
        self._commit(events + [{"step": step, "kind": "intent"}])

    def effect(self, step, outcome):
        events = self._load()
        self._commit(events + [{"step": step, "kind": "effect", "outcome": outcome}])
