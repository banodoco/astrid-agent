"""Plan 32 T5: the generation.machine point, its RunPod contribution and the core driver."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from astrid.core.execution import machine_run
from astrid.core.execution.machine_run import MachineJob, MachineRunCancelled, MachineRunError, run_on_machine
from astrid.core.extensions import registry as reg
from astrid.core.extensions.registry import ExtensionRegistry

REQUIRED = ("claim", "start", "poll", "cancel", "fetch", "release")


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> ExtensionRegistry:
    monkeypatch.setattr(reg, "disabled_ids", lambda: frozenset())
    return ExtensionRegistry().load()


def test_generation_machine_point_is_declared_on_the_agent_side(registry: ExtensionRegistry) -> None:
    point = next(p for p in registry.points() if p.id == "generation.machine")
    assert point.side == "agent"
    assert point.protocol is not None
    assert not [p for p in registry.problems() if "generation.machine" in str(getattr(p, "id", ""))]


def test_registry_resolves_the_runpod_machine_and_checks_the_protocol(registry: ExtensionRegistry) -> None:
    contribution = registry.resolve("generation.machine", "runpod")
    assert contribution.id == "runpod.runpod"
    machine = registry.entry(contribution)  # runs the protocol check; raises if a method is missing
    for name in REQUIRED:
        assert callable(getattr(machine, name)), name
    params = {n: list(inspect.signature(getattr(machine, n)).parameters) for n in REQUIRED}
    assert params["claim"] == ["job", "cancelled"]
    assert params["start"] == ["lease", "job"]
    assert params["poll"] == ["run"]
    assert params["cancel"] == ["run"]
    assert params["fetch"] == ["run", "destination"]
    assert params["release"][:2] == ["lease", "warm"]
    assert machine_run.resolve_machine("runpod", registry) is machine


def test_local_and_attached_are_built_in_not_contributions(registry: ExtensionRegistry) -> None:
    for kind in ("local", "attached"):
        with pytest.raises(MachineRunError, match="built-in"):
            machine_run.resolve_machine(kind, registry)


def test_a_machine_missing_a_method_fails_the_protocol(registry: ExtensionRegistry) -> None:
    point = next(p for p in registry.points() if p.id == "generation.machine")

    class _NoCancel:
        def claim(self, job): ...
        def start(self, lease, job): ...
        def poll(self, run): ...
        def fetch(self, run, destination): ...
        def release(self, lease, warm): ...

    with pytest.raises(reg._LoadFailure):
        registry._check_protocol(point, _NoCancel())


class RentedDirMachine:
    """A further machine kind: an in-memory 'rented' directory, through the same contract."""

    def __init__(self, polls_before_exit: int = 2) -> None:
        self.events: list[tuple] = []
        self.polls_before_exit = polls_before_exit
        self.released: list[bool] = []

    def claim(self, job):
        self.events.append(("claim", job.entry))
        return {"lease": 1, "released": False}

    def start(self, lease, job):
        self.events.append(("start",))
        return {"lease": lease, "job": job, "polls": 0}

    def poll(self, run):
        run["polls"] += 1
        Path(run["job"].progress_path).write_text(f'{{"phase": "executing", "current": {run["polls"]}}}')
        self.events.append(("poll", run["polls"]))
        return {"state": "exited" if run["polls"] >= self.polls_before_exit else "running"}

    def cancel(self, run):
        self.events.append(("cancel",))
        self.release(run["lease"], False)

    def fetch(self, run, destination):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "out.txt").write_text("done")
        self.events.append(("fetch",))
        return {"files": {"out.txt": str(destination / "out.txt")}}

    def release(self, lease, warm):
        if lease["released"]:
            return
        lease["released"] = True
        self.released.append(warm)
        self.events.append(("release", warm))


def _job(tmp_path: Path) -> MachineJob:
    return MachineJob(entry="cpu-image", mode="t2i", policy={"kind": "test"}, inputs={},
                      invocation_id="t:a:1", task_id="t", attempt_id="a", scratch=tmp_path,
                      progress_path=tmp_path / ".astrid-progress.json")


def test_the_test_machine_runs_a_job_through_the_same_contract(registry, tmp_path) -> None:
    point = next(p for p in registry.points() if p.id == "generation.machine")
    machine = RentedDirMachine()
    registry._check_protocol(point, machine)
    fetched = run_on_machine(machine, _job(tmp_path), destination=tmp_path / "outputs",
                             cancelled=lambda: False, sleep=lambda s: None)
    assert fetched["files"] == {"out.txt": str(tmp_path / "outputs" / "out.txt")}
    assert [e[0] for e in machine.events] == ["claim", "start", "poll", "poll", "fetch", "release"]
    assert machine.released == [True]  # success settles warm
    assert '"current": 2' in (tmp_path / ".astrid-progress.json").read_text()


def test_machine_claim_can_receive_the_existing_cancel_callback(tmp_path) -> None:
    received = []

    class WaitingMachine(RentedDirMachine):
        def claim(self, job, *, cancelled=None):
            received.append(cancelled)
            return super().claim(job)

    callback = lambda: False
    machine = WaitingMachine()
    run_on_machine(machine, _job(tmp_path), destination=tmp_path / "outputs",
                   cancelled=callback, sleep=lambda seconds: None)
    assert received == [callback] and machine.released == [True]


def test_a_cancelled_run_stops_the_job_then_releases_cold(tmp_path) -> None:
    machine = RentedDirMachine(polls_before_exit=10)
    polls = iter([False, False, True])
    with pytest.raises(MachineRunCancelled):
        run_on_machine(machine, _job(tmp_path), destination=tmp_path / "outputs",
                       cancelled=lambda: next(polls), sleep=lambda s: None)
    assert [e[0] for e in machine.events] == ["claim", "start", "poll", "cancel", "release"]
    assert machine.released == [False]


def test_a_failed_fetch_releases_cold(tmp_path) -> None:
    machine = RentedDirMachine()

    def broken(run, destination):
        raise RuntimeError("digest mismatch")

    machine.fetch = broken
    with pytest.raises(RuntimeError, match="digest mismatch"):
        run_on_machine(machine, _job(tmp_path), destination=tmp_path / "outputs",
                       cancelled=lambda: False, sleep=lambda s: None)
    assert machine.released == [False]
