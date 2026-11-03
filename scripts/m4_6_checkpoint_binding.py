"""Diagnostic checkpoint correlation predicates; these grant no authority.

Callers must obtain identities and snapshots independently. This module cannot
authenticate a caller, launch a child, protect an export, or prove settlement.
The fixed platform provider and observer admission refusals remain in force.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
import threading
from pathlib import PurePosixPath

from .m4_6_process_observer import ProcessSnapshot


class BindingRefused(ValueError):
    """The diagnostic inputs do not describe the same one-use operation."""


_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_SOURCE = "linux-bounded-proc-systemd-poll"


def _absolute(value: object) -> bool:
    return isinstance(value, str) and bool(value) and PurePosixPath(value).is_absolute()


@dataclass(frozen=True)
class ProcessAnchor:
    """Facts to compare, not proof of their origin or continuing custody."""

    pid: int
    birth: str
    uid: int
    executable: str
    executable_sha256: str
    argv: tuple[str, ...]
    cwd: str

    def __post_init__(self) -> None:
        if (type(self.pid) is not int or self.pid <= 0 or
            not isinstance(self.birth, str) or not self.birth or
            type(self.uid) is not int or self.uid <= 0 or
            not _absolute(self.executable) or not isinstance(self.executable_sha256, str) or
            not _HEX64.fullmatch(self.executable_sha256) or
            type(self.argv) is not tuple or not self.argv or
            any(not isinstance(arg, str) or not arg for arg in self.argv) or
            not _absolute(self.cwd)):
            raise BindingRefused("invalid process anchor")


@dataclass(frozen=True)
class CheckpointBinding:
    slot: str
    candidate: tuple[tuple[str, str], ...]
    run_id: int
    attempt: int
    request_id: str
    plan_sha256: str
    manager_target: str
    manager: ProcessAnchor
    child: ProcessAnchor

    def __post_init__(self) -> None:
        if (not isinstance(self.slot, str) or not self.slot or
            type(self.candidate) is not tuple or len(self.candidate) != 4 or
            any(type(row) is not tuple or len(row) != 2 for row in self.candidate)):
            raise BindingRefused("invalid operation binding")
        if (tuple(row[0] for row in self.candidate) != ("app", "agent", "runtime", "pinokio") or
            any(not isinstance(row[1], str) or not _HEX40.fullmatch(row[1]) for row in self.candidate) or
            type(self.run_id) is not int or self.run_id <= 0 or
            type(self.attempt) is not int or self.attempt <= 0 or
            not isinstance(self.request_id, str) or not _HEX32.fullmatch(self.request_id) or
            not isinstance(self.plan_sha256, str) or not _HEX64.fullmatch(self.plan_sha256) or
            not isinstance(self.manager_target, str) or not self.manager_target or
            type(self.manager) is not ProcessAnchor or type(self.child) is not ProcessAnchor or
            self.manager.pid == self.child.pid or self.manager.uid != self.child.uid):
            raise BindingRefused("invalid operation binding")


def _freeze(snapshot: ProcessSnapshot, binding: CheckpointBinding) -> bytes:
    if (type(snapshot) is not ProcessSnapshot or snapshot.source != _SOURCE or
        snapshot.manager.manager != "systemd-user" or
        snapshot.manager.target != binding.manager_target):
        raise BindingRefused("checkpoint source or target differs")
    return json.dumps(snapshot.record(), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


class CheckpointPair:
    """Compare one operation's checkpoints; never authenticate or admit them.

    ``initial`` and ``final`` must be obtained by the caller from its real
    observer. Bytes are retained at capture time, so later mutation of returned
    objects cannot change either record. Any finish attempt consumes the pair.
    """

    def __init__(self, binding: CheckpointBinding, initial: ProcessSnapshot) -> None:
        if type(binding) is not CheckpointBinding:
            raise BindingRefused("exact operation binding required")
        self._binding = binding
        self._initial = _freeze(initial, binding)
        self._used = False
        self._lock = threading.Lock()

    def finish(self, binding: CheckpointBinding, final: ProcessSnapshot) -> bytes:
        with self._lock:
            if self._used:
                raise BindingRefused("checkpoint pair already consumed")
            self._used = True
        if type(binding) is not CheckpointBinding or binding != self._binding:
            raise BindingRefused("operation or process identity differs")
        final_bytes = _freeze(final, binding)
        return json.dumps({"binding": asdict(binding),
                           "initial": json.loads(self._initial),
                           "final": json.loads(final_bytes),
                           "authority": "none"}, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
