"""A queued task that cannot run must not stop the host claiming other work.

Incident (9 Oct): a chiptune.compose task admitted against the capability
digest a promote had just replaced sat first in the Runtime queue. The Runtime
offers only the oldest queued task, so every claim came back "waiting" and the
pack host claimed nothing (VO, cutouts, visualize) for 25 minutes, logging
nothing. Admission must also stay cheap while retained attempt roots exist.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.execution.guards import ExecutionGuardPolicy
from astrid.pack_api import VERSION


def _write_pack(root: Path, capability_ids: tuple[str, ...]) -> None:
    """One pack.yaml with one action per capability (main's pack layout)."""
    root.mkdir(parents=True)
    actions = "".join(
        f"""  {action}:
    name: {action}
    version: 1.0.0
    short_description: fixture action
    description: fixture action
    invocation:
      kind: command
      command:
        argv: ["{{python_exec}}", "-c", "pass"]
    inputs: []
    outputs: []
    isolation:
      mode: subprocess
      network: false
    metadata:
      adapter_family: cpu
      resource_keys: [cpu]
"""
        for action in (capability_id.split(".", 1)[1] for capability_id in capability_ids)
    )
    (root / "pack.yaml").write_text(
        "schema_version: 3\n"
        "id: fixture\n"
        f'pack_api: "{".".join(map(str, VERSION))}"\n'
        "name: Fixture\n"
        "version: 1.0.0\n"
        "status: active\n"
        "domain: media\n"
        "stability: experimental\n"
        "support: core\n"
        "visibility: visible\n"
        "description: Fixture pack for claim head-of-line tests.\n"
        "capabilities: []\n"
        "actions:\n" + actions,
        encoding="utf-8",
    )


class _QueueRuntime:
    """Offers the oldest queued task among the requested capabilities, like the Runtime."""

    def __init__(self, queue: list[SimpleNamespace]):
        self.queue = queue
        self.requests: list[list[str]] = []

    def claim_next(self, **payload):
        requested = list(payload["capability_ids"])
        self.requests.append(requested)
        for task in self.queue:
            if task.capability_id in requested:
                # Every offered task in this fake is unclaimable; the test
                # only observes which capabilities the host asks for.
                return SimpleNamespace(task=task, waiting_reason=task.waiting_reason)
        return None


def _host(tmp_path: Path, runtime: _QueueRuntime) -> GenericPackHost:
    _write_pack(tmp_path / "fixture", ("fixture.chiptune", "fixture.speech"))
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    host.preflight()
    assert all(record.ready for record in host.capabilities.values()), {
        record.id: record.preflight for record in host.capabilities.values()
    }
    return host


def test_stale_digest_task_blocks_only_its_own_capability(tmp_path, capsys) -> None:
    stale = SimpleNamespace(
        task_id="aa92f0e8",
        capability_id="fixture.chiptune",
        capability_digest="sha256:old",
        waiting_reason="capability_unavailable",
    )
    runtime = _QueueRuntime([stale])
    host = _host(tmp_path, runtime)

    assert host.claim_once() is None
    # The host asked again without the blocked capability, so a queued speech
    # task behind the stale chiptune task would have been offered.
    assert runtime.requests == [
        ["fixture.chiptune", "fixture.speech"],
        ["fixture.speech"],
    ]
    logged = capsys.readouterr().err
    assert "queued task aa92f0e8 (fixture.chiptune) cannot be claimed: capability_unavailable" in logged
    assert "admitted against sha256:old" in logged
    assert "Cancel it and submit it again" in logged

    # Reported once per task and reason, not on every poll.
    host.claim_once()
    assert "aa92f0e8" not in capsys.readouterr().err


def test_capacity_waits_are_not_skipped(tmp_path) -> None:
    busy = SimpleNamespace(
        task_id="t1",
        capability_id="fixture.chiptune",
        capability_digest=None,
        waiting_reason="waiting_for_worker",
    )
    runtime = _QueueRuntime([busy])
    host = _host(tmp_path, runtime)

    assert host.claim_once() is None
    # Executor-wide capacity applies to every capability; asking again is pointless.
    assert runtime.requests == [["fixture.chiptune", "fixture.speech"]]


def test_skips_are_bounded_when_every_capability_is_blocked(tmp_path) -> None:
    queue = [
        SimpleNamespace(
            task_id=f"t{index}",
            capability_id=capability_id,
            capability_digest="sha256:old",
            waiting_reason="insufficient_storage",
        )
        for index, capability_id in enumerate(("fixture.chiptune", "fixture.speech"))
    ]
    runtime = _QueueRuntime(queue)
    host = _host(tmp_path, runtime)

    assert host.claim_once() is None
    assert runtime.requests == [["fixture.chiptune", "fixture.speech"], ["fixture.speech"]]


def test_admission_with_large_retained_attempt_roots_stays_in_milliseconds(
    tmp_path, monkeypatch
) -> None:
    policy = ExecutionGuardPolicy()
    for index in range(5):
        root = tmp_path / f"astrid-attempt-retained-{index}"
        frames = root / ".render-service" / "react-motion-render"
        frames.mkdir(parents=True)
        for frame in range(2000):
            (frames / f"element-{frame:04d}.png").write_bytes(b"x")
        # A retained root keeps its charge while it exists (keep_attempt).
        policy.evidence_budget.account(str(root.resolve()), 260 * 1024**2, policy.evidence_cap_bytes * 4)

    def no_walk(*_args, **_kwargs):
        raise AssertionError("admission must not walk attempt roots")

    assert_budget_available = policy.assert_budget_available
    checks = 0

    def count_budget_check(_policy):
        nonlocal checks
        checks += 1
        return assert_budget_available()

    monkeypatch.setattr(ExecutionGuardPolicy, "assert_budget_available", count_budget_check)
    # Keep the walk guard active for the admission calls only. Pytest's
    # tmp_path teardown scans the directory tree after the test returns.
    with monkeypatch.context() as admission_patch:
        admission_patch.setattr(Path, "rglob", no_walk)
        admission_patch.setattr("os.scandir", no_walk)
        admission_patch.setattr("os.walk", no_walk)
        for _ in range(100):
            policy.assert_budget_available()
        assert checks == 100


def test_live_evidence_sample_hashes_unchanged_inputs_once(tmp_path, monkeypatch) -> None:
    policy = ExecutionGuardPolicy()
    inputs = tmp_path / "attempt" / "managed-objects"
    inputs.mkdir(parents=True)
    for index in range(20):
        (inputs / f"{index:04d}").write_bytes(b"input" * 1000)
    baseline = policy.immutable_input_baseline(inputs)
    # Main hashes inputs through hashlib.file_digest (not Path.read_bytes), so
    # count digests of input files; the baseline above is taken before patching.
    reads: list[str] = []
    original_digest = hashlib.file_digest

    def counting_digest(stream, *args, **kwargs):
        reads.append(stream.name)
        return original_digest(stream, *args, **kwargs)

    monkeypatch.setattr(hashlib, "file_digest", counting_digest)
    for _ in range(10):
        measurement = policy.evidence_measurement(tmp_path / "attempt", immutable_inputs=baseline)
        assert measurement["immutable_file_count"] == 20
        assert measurement["observed_bytes"] == 0
    assert len(reads) == 20

    # A changed input is re-hashed and charged as generated bytes.
    target = inputs / "0000"
    time.sleep(0.01)
    target.write_bytes(b"changed")
    measurement = policy.evidence_measurement(tmp_path / "attempt", immutable_inputs=baseline)
    assert measurement["observed_bytes"] == len(b"changed")


@pytest.mark.parametrize("interval", [0.5])
def test_live_guard_interval_is_bounded(interval) -> None:
    from astrid.core.execution import generic_host

    assert generic_host._LIVE_GUARD_INTERVAL_SECONDS == interval
