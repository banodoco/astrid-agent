"""Durable table decisions; callers supply serialization, storage and executors."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Generic, TypeVar

S = TypeVar("S", bound=Hashable)
E = TypeVar("E", bound=Hashable)
A = TypeVar("A")
Data = Mapping[str, object]


class UndefinedTransition(ValueError):
    """An event has no authority in this state."""


@dataclass(frozen=True)
class Record(Generic[S]):
    state: S
    data: Data = field(default_factory=dict)
    revision: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))


@dataclass(frozen=True)
class Outcome(Generic[S, A]):
    """A policy may resolve a conditional row using durable facts and evidence."""

    next_state: S
    action: A
    effects: tuple[str, ...]
    data: Data


@dataclass(frozen=True)
class Rule(Generic[S, A]):
    next_state: S
    action: A
    effects: tuple[str, ...] = ()
    resolve: Callable[[Data, Data], Outcome[S, A]] | None = None


@dataclass(frozen=True)
class Decision(Generic[S, A]):
    record: Record[S]
    action: A
    effects: tuple[str, ...]


def transition(
    table: Mapping[tuple[S, E], Rule[S, A]],
    record: Record[S],
    event: E,
    *,
    persist: Callable[[Record[S], tuple[str, ...]], None],
    retry: Callable[[Record[S], E, Data], None],
    changes: Data | None = None,
) -> Decision[S, A]:
    """Persist a complete decision before returning its action.

    Under a short lock, the caller reads the current record, calls this function
    and atomically replaces record AND effects with an operation/revision check.
    Effects are write-ahead data, never callbacks. Executors run after unlocking.
    A decision can be an intent requiring a later admission event; the policy
    defines its revocation and dispatch rules. This function performs no I/O
    beyond the supplied persistence and retry callbacks.

    Every invocation persists, even a same-state retry. Persistence failure
    schedules retry with the ORIGINAL record, event and evidence (including late
    results). An undefined pair or invalid proof returns no authority. Storage
    annotations belong in effects so their failure uses this same retry boundary.
    """
    try:
        rule = table[record.state, event]
    except KeyError as exc:
        raise UndefinedTransition(f"undefined transition: {record.state!r}, {event!r}") from exc
    evidence = changes if changes is not None else {}
    outcome = (rule.resolve(record.data, evidence) if rule.resolve is not None else
               Outcome(rule.next_state, rule.action, rule.effects, {**record.data, **evidence}))
    updated = Record(outcome.next_state, outcome.data, record.revision + 1)
    persisted = False
    try:
        persist(updated, outcome.effects)
        persisted = True
    finally:
        if not persisted:
            retry(record, event, evidence)
    return Decision(updated, outcome.action, outcome.effects)
