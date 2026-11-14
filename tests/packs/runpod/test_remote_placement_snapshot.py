"""P6 placement hooks: restore before install, detached publish, bounded publish wait at release.

The pod is the command-pattern fake from test_remote_placement; the engine_snapshot module is
never imported here, only its fixed CLI and its final ``ENGINE_SNAPSHOT_JSON=`` line are simulated.
"""

from __future__ import annotations

import json
import re
import shlex

import pytest

from astrid.packs.runpod import machine as rm
from astrid.packs.runpod import remote_placement as rp
from astrid.packs.runpod.remote_placement import Claim, Limits, RemotePlacement, RunPodInstallation
from tests.packs.runpod.test_generation_preparation import retained as retained  # noqa: F401
from tests.packs.runpod.test_machine import machine_case as machine_case  # noqa: F401
from tests.packs.runpod.test_remote_placement import FakePod, _provider
from tests.packs.runpod.test_remote_placement import sources as sources  # noqa: F401
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401
from tests.packs.runpod.test_warm_session import (
    lease_selection_case as lease_selection_case,  # noqa: F401
)
from tests.packs.runpod.test_warm_session import small_lease as small_lease  # noqa: F401

VOLUME = {"mount_path": "/workspace", "size_gb": 200}
RECEIPT = f"{rp.ENGINE_ROOT}/.astrid-engine/install.json"
RUNTIME = f"{rp.ENGINE_ROOT}/runtime"


def _line(**body) -> str:
    return rp.SNAPSHOT_MARK + json.dumps(body) + "\n"


class SnapshotPod(FakePod):
    """FakePod plus container-disk paths and the snapshot module's CLI."""

    def __init__(self, files, marker):
        super().__init__(files, marker)
        self.present: set[str] = set()
        # The module's own result shapes: no reason on success, report rows and a partial count.
        self.restore = (0, _line(command="restore", action="restored", durations={"extract": 41.5}))
        self.report = (0, _line(command="report", action="reported", durations={"total": 0.2},
                                total_bytes=7 << 30, partials=1, cleaned=0,
                                identities=[{"identity": "a" * 64, "bytes": 6 << 30},
                                            {"identity": "b" * 64, "bytes": 1 << 30}]))
        self.alive: list[int] = []  # exit codes of successive liveness checks; empty means exited
        self.restore_raises: BaseException | None = None
        self.waits: list[int] = []
        self.timeouts: list[float] = []

    def exec(self, command, timeout=600):
        if "engine_snapshot restore" in command:
            self.exec_log.append(command)
            if self.restore_raises is not None:
                raise self.restore_raises
            code, out = self.restore
            return code, out, ""
        if "engine_snapshot publish" in command:
            self.exec_log.append(command)
            return 0, "4242\n", ""
        if "engine_snapshot report" in command:
            self.exec_log.append(command)
            code, out = self.report
            return code, out, ""
        if "/proc/4242/stat" in command:
            self.exec_log.append(command)
            self.timeouts.append(timeout)
            wait = re.search(r"-lt (\d+)", command)
            if wait is None:  # a single liveness check
                return (self.alive.pop(0) if self.alive else 1), "", ""
            # One on-pod bounded wait: one second per check, exit 3 if still running at the bound.
            self.waits.append(int(wait.group(1)))
            for _ in range(int(wait.group(1)) + 1):
                if not self.alive or self.alive.pop(0) != 0:
                    return 0, "", ""
            return 3, "", ""
        if command.startswith("test -e "):
            self.exec_log.append(command)
            return (0 if shlex.split(command)[2] in self.present else 1), "", ""
        return super().exec(command, timeout)


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def _prepare(tmp_path, sources, *, volume=VOLUME, pod=None, clock=None):
    provider = _provider(sources)
    pod = pod or SnapshotPod({}, provider.marker)
    clock = clock or Clock()
    placement = RemotePlacement(provider, limits=Limits(poll_seconds=0), source_root=sources,
                                clock=clock, sleep=clock.sleep)
    state = placement.prepare_claimed(Claim("pod-1", pod, {}), out=tmp_path / "out",
                                      deadline_seconds=3600, volume=volume)
    return placement, state, pod


def _index(log, needle):
    return next(i for i, command in enumerate(log) if needle in command)


def _notes(state, step):
    return [entry for entry in state.steps if entry["step"] == step]


def test_volume_and_empty_root_restores_before_status_and_still_installs(tmp_path, sources):
    _placement, state, pod = _prepare(tmp_path, sources)
    restore = pod.exec_log[_index(pod.exec_log, "engine_snapshot restore")]
    assert _index(pod.exec_log, "engine_snapshot restore") < _index(pod.exec_log, "engines status")
    assert f"{rp.POD_VENV}/bin/python -I -m astrid.packs.runpod.engine_snapshot restore" in restore
    argv = shlex.split(restore)
    assert argv[argv.index("--root") + 1] == rp.ENGINE_ROOT
    assert argv[argv.index("--cache") + 1] == "/workspace/astrid/engine-envs/comfyui"
    assert argv[argv.index("--engine") + 1] == "comfyui"
    assert argv[argv.index("--platform") + 1] == "linux-x86_64-cu130"
    assert 0 < int(argv[argv.index("--deadline") + 1]) <= rp.SNAPSHOT_RESTORE_SECONDS
    # Restore never replaces the install: the unchanged install job still runs.
    assert any("engines install" in command for command in pod.job_log)
    assert not any("engine_snapshot" in command for command in pod.job_log)


def test_restored_then_install_and_status_healthy_flow(tmp_path, sources):
    _placement, state, _pod = _prepare(tmp_path, sources)
    assert [(entry["step"], entry["status"]) for entry in state.steps] == [
        ("bootstrap", "done"), ("engine_snapshot_restore", "restored"), ("install", "done"),
        ("engine_snapshot_publish", "started")]
    restore = _notes(state, "engine_snapshot_restore")[0]
    assert restore["reason"] == "none" and restore["durations"] == {"extract": 41.5}


def test_no_volume_has_no_restore_and_no_publish(tmp_path, sources):
    placement, state, pod = _prepare(tmp_path, sources, volume=None)
    placement.settle_engine_snapshot(state)
    assert not any("engine_snapshot" in command for command in pod.exec_log)
    assert not any(command.startswith("test -e ") for command in pod.exec_log)
    assert [entry["step"] for entry in state.steps] == ["bootstrap", "install"]


def test_installation_root_other_than_engine_root_never_restores_or_publishes(tmp_path, sources):
    provider = _provider(sources)
    pod = SnapshotPod({}, provider.marker)
    placement = RemotePlacement(provider, limits=Limits(poll_seconds=0), source_root=sources)
    state = placement.begin_claimed(Claim("pod-1", pod, {}), out=tmp_path / "out", deadline_seconds=600)
    state.volume = VOLUME
    state.installation = RunPodInstallation(compatibility={}, root="/root/astrid/engines/other")
    placement._restore_engine_snapshot(state)
    placement._start_snapshot_publish(state)
    assert pod.exec_log == [] and state.steps == []
    # The canonical installation root is eligible.
    state.installation = RunPodInstallation(compatibility={})
    placement._restore_engine_snapshot(state)
    assert any("engine_snapshot restore" in command for command in pod.exec_log)


@pytest.mark.parametrize("existing,reason", [(RECEIPT, "receipt_present"), (RUNTIME, "runtime_present")])
def test_existing_receipt_or_runtime_skips_restore(tmp_path, sources, existing, reason):
    provider = _provider(sources)
    pod = SnapshotPod({}, provider.marker)
    pod.present.add(existing)
    _placement, state, pod = _prepare(tmp_path, sources, pod=pod)
    assert not any("engine_snapshot restore" in command for command in pod.exec_log)
    assert _notes(state, "engine_snapshot_restore") == [
        {"step": "engine_snapshot_restore", "status": "skipped", "reason": reason,
         "at": _notes(state, "engine_snapshot_restore")[0]["at"]}]
    assert any("engines install" in command for command in pod.job_log)


@pytest.mark.parametrize("reply,raises,reason", [
    ((3, "Traceback: boom\n"), None, "restore_exit_nonzero"),
    ((0, "garbage\nENGINE_SNAPSHOT_JSON={not json\n"), None, "restore_output_unparsable"),
    ((0, "no result line at all\n"), None, "restore_output_unparsable"),
    ((0, ""), TimeoutError("exec timed out"), "restore_failed"),
])
def test_failed_restore_falls_through_to_the_unchanged_install(tmp_path, sources, reply, raises, reason):
    provider = _provider(sources)
    pod = SnapshotPod({}, provider.marker)
    pod.restore, pod.restore_raises = reply, raises
    _placement, state, pod = _prepare(tmp_path, sources, pod=pod)
    note = _notes(state, "engine_snapshot_restore")[0]
    assert (note["status"], note["reason"]) == ("skipped", reason)
    assert ("install", "done") in [(entry["step"], entry["status"]) for entry in state.steps]
    install = next(command for command in pod.job_log if "engines install" in command)
    assert install.startswith(f"{rp.POD_VENV}/bin/astrid engines install comfyui --placement local")


def test_publish_is_detached_after_install_and_returns_immediately(tmp_path, sources):
    _placement, state, pod = _prepare(tmp_path, sources)
    publish = pod.exec_log[_index(pod.exec_log, "engine_snapshot publish")]
    assert _index(pod.exec_log, "engine_snapshot publish") > _index(pod.exec_log, "engines status")
    assert "nohup setsid " in publish and " & echo $! > " in publish
    assert f"> {rp.SNAPSHOT_PUBLISH_LOG} 2>&1 < /dev/null &" in publish
    assert rp.SNAPSHOT_PUBLISH_PID in publish
    assert "--mount /workspace --volume-size-gb 200" in publish
    # A plain exec, not a polled job: nothing waits for publish during preparation.
    assert not any("engine_snapshot" in command for command in pod.job_log)
    assert state.snapshot_publish_pid == 4242
    assert _notes(state, "engine_snapshot_publish")[0]["pid"] == 4242
    assert not any("/proc/4242" in command for command in pod.exec_log)


def test_publish_without_declared_size_omits_mount_flags(tmp_path, sources):
    _placement, _state, pod = _prepare(tmp_path, sources, volume={"mount_path": "/workspace", "size_gb": None})
    publish = pod.exec_log[_index(pod.exec_log, "engine_snapshot publish")]
    assert "--mount" not in publish and "--volume-size-gb" not in publish


def test_healthy_install_skipped_still_attempts_publish(tmp_path, sources):
    provider = _provider(sources)
    pod = SnapshotPod({}, provider.marker)
    pod.installed = True
    pod.present.add(RECEIPT)
    _placement, state, pod = _prepare(tmp_path, sources, pod=pod)
    assert [(entry["step"], entry["status"]) for entry in state.steps][-2:] == [
        ("install", "skipped"), ("engine_snapshot_publish", "started")]


def test_release_waits_for_publish_then_reports_totals(tmp_path, sources):
    placement, state, pod = _prepare(tmp_path, sources)
    pod.alive = [0, 0, 0]  # still running for three checks, then exited
    placement.settle_engine_snapshot(state)
    # Exactly one bounded on-pod wait (each exec is a job record: never poll from the host).
    assert pod.waits == [rp.SNAPSHOT_PUBLISH_WAIT_SECONDS - rp.SNAPSHOT_REPORT_MIN_SECONDS
                         - rp.SNAPSHOT_SLACK_SECONDS] and len(
        [c for c in pod.exec_log if "/proc/4242" in c]) == 1
    assert not _notes(state, "engine_snapshot_publish")[1:]
    report = pod.exec_log[_index(pod.exec_log, "engine_snapshot report")]
    assert _index(pod.exec_log, "engine_snapshot report") > max(
        i for i, command in enumerate(pod.exec_log) if "/proc/4242" in command)
    assert "--cache /workspace/astrid/engine-envs/comfyui" in report
    note = _notes(state, "engine_snapshot_report")[0]
    assert (note["status"], note["total_bytes"], note["identities"], note["partials"]) == (
        "reported", 7 << 30, 2, 1)
    # One settlement per publish.
    placement.settle_engine_snapshot(state)
    assert len(_notes(state, "engine_snapshot_report")) == 1


def test_release_wait_is_bounded_and_leaves_publish_unfinished(tmp_path, sources):
    placement, state, pod = _prepare(tmp_path, sources)
    pod.alive = [0] * 10_000  # never exits
    placement.settle_engine_snapshot(state)
    assert rp.SNAPSHOT_PUBLISH_WAIT_SECONDS == 180 and pod.waits == [160]
    assert _notes(state, "engine_snapshot_publish")[-1]["status"] == "publish_unfinished"
    # The release continues: report still runs and its totals are noted.
    assert _notes(state, "engine_snapshot_report")[0]["status"] == "reported"


def test_warm_settle_does_not_wait_and_paid_budget_bounds_the_wait(tmp_path, sources):
    placement, state, pod = _prepare(tmp_path, sources)
    pod.alive = [0] * 100
    placement.settle_engine_snapshot(state, wait_seconds=0)
    assert pod.waits == []
    assert _notes(state, "engine_snapshot_publish")[-1]["status"] == "still_running"
    assert not _notes(state, "engine_snapshot_report")
    placement, state, pod = _prepare(tmp_path, sources)
    pod.alive = [0] * 100
    placement.settle_engine_snapshot(state, budget_seconds=12)
    assert pod.waits == [0]
    assert _notes(state, "engine_snapshot_publish")[-1]["status"] == "publish_unfinished"


@pytest.mark.parametrize("report", [(1, "boom"), (0, "not a result"), None])
def test_report_or_transport_failure_never_raises(tmp_path, sources, report):
    placement, state, pod = _prepare(tmp_path, sources)
    if report is None:
        def broken(command, timeout=600):
            raise ConnectionError("transport gone")
        pod.exec = broken
    else:
        pod.report = report
    placement.settle_engine_snapshot(state)
    assert _notes(state, "engine_snapshot_report")[-1] == {
        "step": "engine_snapshot_report", "status": "skipped", "reason": "report_failed",
        "at": _notes(state, "engine_snapshot_report")[-1]["at"]}


def test_machine_release_settles_snapshot_before_stop_and_without_waiting_when_warm(
        machine_case, monkeypatch):
    """The release wait lives at the last pod step of machine release, before owner.release stops the pod."""
    from tests.packs.runpod.test_machine import _claim_machine

    c = machine_case
    _claim_machine(c)
    calls = []

    def settle(state, *, wait_seconds, budget_seconds):
        calls.append((wait_seconds, budget_seconds is not None and budget_seconds > 0, list(c.deletes)))
        raise RuntimeError("snapshot bookkeeping broke")  # must never fail release

    monkeypatch.setattr(c.placement, "settle_engine_snapshot", settle)
    warm = c.machine.claim(c.job())
    c.machine.release(warm, True)
    assert c.read()["lease"]["state"] == "idle"
    cold = c.machine.claim(c.job(2))
    c.machine.release(cold, False)
    assert c.deletes == ["pod-1"]
    assert calls == [(0, True, []), (rp.SNAPSHOT_PUBLISH_WAIT_SECONDS, True, [])]


class _ExecClock:
    """Charges every pod exec its full transport timeout: the worst case a release can see."""

    def __init__(self, pod, clock):
        self.pod, self.clock, self.charged = pod, clock, []
        inner = pod.exec

        def exec_(command, timeout=600):
            self.charged.append(timeout)
            self.clock.now += timeout
            return inner(command, timeout)

        pod.exec = exec_


@pytest.mark.parametrize("alive", [[0] * 10_000, [0, 0, 0]])
def test_release_settlement_is_one_bound_including_report(tmp_path, sources, alive):
    """R4: wait + report + transport slack together stay within SNAPSHOT_PUBLISH_WAIT_SECONDS."""
    clock = Clock()
    placement, state, pod = _prepare(tmp_path, sources, clock=clock)
    pod.alive = alive
    charged = _ExecClock(pod, clock)
    start = clock.now
    placement.settle_engine_snapshot(state)
    assert clock.now - start <= rp.SNAPSHOT_PUBLISH_WAIT_SECONDS == 180
    assert sum(charged.charged) <= 180
    assert _notes(state, "engine_snapshot_report")  # the report still fits inside the bound


def test_release_skips_report_when_under_ten_seconds_remain(tmp_path, sources):
    clock = Clock()
    placement, state, pod = _prepare(tmp_path, sources, clock=clock)
    pod.alive = [0] * 10_000
    charged = _ExecClock(pod, clock)
    placement.settle_engine_snapshot(state, budget_seconds=15)
    assert not any("engine_snapshot report" in command for command in pod.exec_log)
    assert sum(charged.charged) <= 15


@pytest.mark.parametrize("failure", ["job", "digest"])
def test_failed_job_release_never_settles_snapshot(machine_case, monkeypatch, failure):
    """R3: settlement runs on the success path only; a failed job's release never waits on publish."""
    from tests.packs.runpod.test_machine import _claim_machine

    c = machine_case
    _claim_machine(c)
    calls = []
    monkeypatch.setattr(c.placement, "settle_engine_snapshot",
                        lambda state, **kw: calls.append(kw))
    lease = c.machine.claim(c.job())
    error = rm.MachineError("job failed") if failure == "job" else rp.DigestMismatch("digest")
    try:
        c.machine.release(lease, False, failure=error)
    except Exception:  # noqa: BLE001 - classification may surface; only the wait matters here
        pass
    assert calls == []
