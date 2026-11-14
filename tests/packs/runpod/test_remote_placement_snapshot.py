"""P6 placement hooks: restore before install, detached publish, bounded publish wait at release.

The pod is the command-pattern fake from test_remote_placement; the engine_snapshot module is
never imported here, only its fixed CLI and its final ``ENGINE_SNAPSHOT_JSON=`` line are simulated.
"""

from __future__ import annotations

import json
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
        self.restore_raises: BaseException | None = None

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
    _placement, state, pod = _prepare(tmp_path, sources, volume=None)
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
    # Report (totals, stale-partial cleanup) follows publish inside the same detached chain.
    chain = shlex.split(publish.split("nohup setsid sh -c ", 1)[1])[0]
    first, second = chain.split("; ")
    assert "engine_snapshot publish" in first and "engine_snapshot report" in second
    assert f"--deadline {rp.SNAPSHOT_REPORT_SECONDS}" in second
    # A plain exec, not a polled job: nothing waits for publish during preparation or release.
    assert not any("engine_snapshot" in command for command in pod.job_log)
    assert not hasattr(state, "snapshot_publish_pid")
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


@pytest.mark.parametrize("failure", [None, "job", "digest"])
def test_release_never_touches_the_snapshot(machine_case, failure):
    """W113/W116/W119: release has no snapshot step at all, on success or failure, so nothing
    snapshot-related can delay, skip or fail the pod release."""
    from tests.packs.runpod.test_machine import _claim_machine

    c = machine_case
    _claim_machine(c)
    lease = c.machine.claim(c.job())
    lease.state.volume = {"mount_path": "/workspace", "size_gb": 300}
    seen = []
    inner = lease.claim.ops.exec

    def exec_(command, timeout=600, *args, **kwargs):
        seen.append(command)
        return inner(command, timeout, *args, **kwargs)

    lease.claim.ops.exec = exec_
    error = {None: None, "job": rm.MachineError("job failed"), "digest": rp.DigestMismatch("digest")}[failure]
    try:
        c.machine.release(lease, False, failure=error) if error else c.machine.release(lease, False)
    except Exception:  # noqa: BLE001 - classification may surface; only the snapshot steps matter here
        pass
    if failure is None:
        assert c.deletes == ["pod-1"]  # failure paths keep compose's own release classification
    assert not any("engine_snapshot" in command or "/proc/" in command for command in seen)
    assert not hasattr(c.placement, "settle_engine_snapshot")


def test_running_or_stalled_publish_has_no_effect_on_release(machine_case):
    """A publish still running (or a pod-side snapshot command that would hang forever) neither
    delays nor changes the release: release never issues one, so the stall is never reached."""
    import threading
    import time as _time

    from tests.packs.runpod.test_machine import _claim_machine

    c = machine_case
    _claim_machine(c)
    lease = c.machine.claim(c.job())
    lease.state.volume = {"mount_path": "/workspace", "size_gb": 300}
    lease.state.note("engine_snapshot_publish", "started", pid=4242)  # still running on the pod
    never = threading.Event()
    inner = lease.claim.ops.exec

    def exec_(command, timeout=600, *args, **kwargs):
        if "engine_snapshot" in command or "/proc/" in command:
            never.wait(3600)  # would stall release for an hour if release ever touched it
        return inner(command, timeout, *args, **kwargs)

    lease.claim.ops.exec = exec_
    started = _time.monotonic()
    try:
        c.machine.release(lease, False)
    finally:
        never.set()
    assert _time.monotonic() - started < 10
    assert c.deletes == ["pod-1"] and lease.released
