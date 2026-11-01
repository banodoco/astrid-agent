"""Structural tests for B's local, intentionally non-authoritative observer."""

from __future__ import annotations

import platform
import sys
from pathlib import Path

import pytest

from scripts.m4_6_process_observer import (
    ManagerIdentity,
    NativeServiceObservationTarget,
    ObservationRefused,
    ObservationUnavailable,
    ProcessSnapshot,
    ProcessTransitionObserver,
)

_OS_NAME = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}[platform.system()]
_MANAGER = {"linux": "systemd-user", "macos": "launchd", "windows": "task-scheduler"}[_OS_NAME]
_TARGET = {
    "linux": "astrid.service",
    "macos": "gui/501/haus.astrid.runtime",
    "windows": r"\Astrid Runtime",
}[_OS_NAME]


def _target() -> NativeServiceObservationTarget:
    return NativeServiceObservationTarget(
        os_name=_OS_NAME,
        manager=_MANAGER,
        manager_target=_TARGET,
        definition_path=None,
        allowed_executables=(Path(sys.executable),),
    )


def _absent(*, sequence: int | None = None) -> ProcessSnapshot:
    return ProcessSnapshot(
        source="structural-test-snapshot",
        manager=ManagerIdentity(
            manager=_MANAGER,
            target=_TARGET,
            state="absent",
            instance_id=None,
            definition_path=None,
            runtime_pid=None,
        ),
        runtime=None,
        descendants=(),
        unbound_processes=(),
        source_sequence=sequence,
    )


def test_polling_observer_records_unknown_continuity_and_refuses_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = ProcessTransitionObserver(_target())
    monkeypatch.setattr(
        observer._source, "capture", _absent
    )  # Structural test only; not public API.

    observer.arm_initial_absence()
    observer.poll()

    assert any(event.kind == "event-source-unavailable" for event in observer.events)
    assert observer.report()["continuity"] == "not-proven-bounded-polling"
    with pytest.raises(ObservationRefused, match="gap-free continuity"):
        observer.assert_authorizable("authorization")


def test_sequence_discontinuity_is_a_gap_and_refuses_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = ProcessTransitionObserver(_target())
    snapshots = iter((_absent(sequence=41), _absent(sequence=43)))
    monkeypatch.setattr(observer._source, "capture", lambda: next(snapshots))

    observer.arm_initial_absence()
    observer.poll()

    assert any(event.kind == "event-sequence-discontinuity" for event in observer.events)
    assert any(event.status == "gap" for event in observer.events)
    with pytest.raises(ObservationRefused, match="sequence discontinuity"):
        observer.assert_authorizable("finalization")


def test_lost_source_records_explicit_gap_and_never_becomes_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = ProcessTransitionObserver(_target())
    monkeypatch.setattr(observer._source, "capture", _absent)
    observer.arm_initial_absence()

    def unavailable() -> ProcessSnapshot:
        raise ObservationUnavailable("permission denied by test source")

    monkeypatch.setattr(observer._source, "capture", unavailable)
    with pytest.raises(ObservationUnavailable, match="permission denied"):
        observer.poll()

    assert any(
        event.kind == "source-unavailable" and event.status == "gap" for event in observer.events
    )
    assert observer.report()["authorization"] == "refused"


def test_bounded_ledger_records_overflow_then_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    observer = ProcessTransitionObserver(_target(), max_events=8)
    monkeypatch.setattr(observer._source, "capture", _absent)
    observer.arm_initial_absence()
    for _index in range(5):
        observer.poll()

    assert observer.events[-1].kind == "ledger-overflow"
    assert observer.events[-1].status == "overflow"
    with pytest.raises(ObservationRefused, match="overflowed"):
        observer.assert_authorizable("authorization")
