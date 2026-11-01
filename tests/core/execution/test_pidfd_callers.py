"""Agent retained-handle callers work when Linux Python omits pidfd wrappers."""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from runtime_protocol import platform

from astrid.core.execution import custody_broker as custody
from astrid.core.execution import process_group as groups


@pytest.fixture
def without_stdlib_pidfds(monkeypatch):
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    assert not hasattr(os, "pidfd_open")
    assert not hasattr(signal, "pidfd_send_signal")


@pytest.fixture
def runtime_pidfd_provider(without_stdlib_pidfds, monkeypatch):
    monkeypatch.setattr(custody.sys, "platform", "linux")
    provider = SimpleNamespace(pidfd_open=Mock(return_value=73), pidfd_send_signal=Mock())
    monkeypatch.setattr(platform, "pidfd_open", provider.pidfd_open)
    monkeypatch.setattr(platform, "pidfd_send_signal", provider.pidfd_send_signal)
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("numeric PID fallback"))
    monkeypatch.setattr(os, "killpg", lambda *_: pytest.fail("numeric process-group fallback"), raising=False)
    monkeypatch.setattr(custody, "process_control_report", lambda: {"ok": True, "pidfd": "available"})
    return provider


@pytest.fixture
def sandbox_mode(monkeypatch):
    """Sandbox process control with pidfd_open answering ENOSYS, as in OpenShell."""
    monkeypatch.setattr(platform, "_process_control", "sandbox")
    monkeypatch.setattr(platform, "_fallbacks_noted", set())

    def enosys(*_args):
        raise OSError(errno.ENOSYS, "pidfd_open failed: Function not implemented")

    monkeypatch.setattr(platform, "pidfd_open", enosys)
    monkeypatch.setattr(platform, "pidfd_send_signal", lambda *_: pytest.fail("pidfd signal without a pidfd"))
    monkeypatch.setattr(custody, "process_control_report", lambda: {"ok": True, "signalling": "pid-with-start-time-check"})


@pytest.mark.parametrize("reason", [None, "Runtime cannot stop processes safely here (pidfd: enosys, ...); install with --sandbox"])
def test_custody_launch_refuses_when_the_probe_cannot_signal(tmp_path, monkeypatch, runtime_pidfd_provider, reason):
    report = {"ok": False, "pidfd": "enosys"}
    if reason:
        report["reason"] = reason
    monkeypatch.setattr(custody, "process_control_report", lambda: report)
    owner = custody.ProcessCustody(scope_root=tmp_path)
    with pytest.raises(custody.CustodyError, match="--sandbox" if reason else "kernel custody is unavailable"):
        owner.prepare_launch([sys.executable, "-c", "pass"], start_new_session=True)
    runtime_pidfd_provider.pidfd_open.assert_not_called()
    runtime_pidfd_provider.pidfd_send_signal.assert_not_called()


@pytest.mark.parametrize("group_kill", ["allowed", "denied"])
def test_custody_retains_runtime_pidfd_and_preserves_signal_fences(tmp_path, monkeypatch, runtime_pidfd_provider, group_kill):
    provider = runtime_pidfd_provider
    monkeypatch.setattr(platform, "_process_control", "strict")
    monkeypatch.setattr(custody, "process_control_report", lambda: {
        "process_control": "strict", "pidfd": "available", "signalling": "pidfd",
        "process_group_kill": group_kill,
        "group_signalling": "killpg" if group_kill == "allowed" else "unavailable",
        "ok": group_kill == "allowed",
    })
    owner = custody.ProcessCustody(scope_root=tmp_path)
    argv = [sys.executable, "-c", "pass"]
    assert owner.prepare_launch(argv, start_new_session=True) == (argv, {})
    provider.pidfd_open.assert_not_called()  # Preflight acquires no incidental handle.
    monkeypatch.setattr(groups, "process_birth_identity", lambda pid=None: "actor" if pid is None else "child")
    monkeypatch.setattr(groups, "held_child_birth_identity", lambda _pid: "child")
    inheritable, closed = Mock(), Mock()
    monkeypatch.setattr(custody.os, "set_inheritable", inheritable)
    monkeypatch.setattr(custody.os, "close", closed)
    owner.bind_launch(SimpleNamespace(pid=123))
    assert owner._handles[123] == ("child", 73)
    provider.pidfd_open.assert_called_once_with(123)
    inheritable.assert_called_once_with(73, False)
    with pytest.raises(custody.CustodyError, match="retained exact process"):
        owner.signal(123, "replacement", platform.TERM)
    monkeypatch.setattr(groups, "process_birth_identity", lambda *_: "replacement-actor")
    with pytest.raises(custody.CustodyError, match="actor incarnation changed"):
        owner.signal(123, "child", platform.TERM)
    provider.pidfd_send_signal.assert_not_called()
    monkeypatch.setattr(groups, "process_birth_identity", lambda *_: "actor")
    owner.signal(123, "child", platform.TERM)
    provider.pidfd_send_signal.assert_called_once_with(73, platform.TERM)
    owner.release_pidfds()
    closed.assert_called_once_with(73)


@pytest.mark.parametrize("failure", [errno.ENOSYS, errno.EPERM, errno.ESRCH])
def test_custody_open_failure_cannot_acquire_signal_authority(tmp_path, monkeypatch, runtime_pidfd_provider, failure):
    provider = runtime_pidfd_provider
    provider.pidfd_open.side_effect = OSError(failure, "native pidfd failure")
    owner = custody.ProcessCustody(scope_root=tmp_path)
    monkeypatch.setattr(groups, "process_birth_identity", lambda *_: "child")
    monkeypatch.setattr(groups, "held_child_birth_identity", lambda _pid: "child")
    with pytest.raises(OSError) as error:
        owner.bind_launch(SimpleNamespace(pid=123))
    assert error.value.errno == failure
    assert not owner._handles and owner._uncertain is error.value
    provider.pidfd_send_signal.assert_not_called()


def test_custody_birth_drift_after_open_closes_unadmitted_handle(tmp_path, monkeypatch, runtime_pidfd_provider):
    owner = custody.ProcessCustody(scope_root=tmp_path)
    births = iter(("child", "replacement"))
    monkeypatch.setattr(groups, "process_birth_identity", lambda *_: next(births))
    closed = Mock()
    monkeypatch.setattr(custody.os, "close", closed)
    owner.register(123, "child")
    assert not owner._handles
    closed.assert_called_once_with(73)
    runtime_pidfd_provider.pidfd_send_signal.assert_not_called()


@pytest.mark.parametrize("entry", ["register", "bind-launch"])
@pytest.mark.parametrize("stage", ["identity", "inheritability"])
def test_custody_post_open_failure_closes_untransferred_handle(
    tmp_path, monkeypatch, runtime_pidfd_provider, entry, stage,
):
    provider = runtime_pidfd_provider
    owner = custody.ProcessCustody(scope_root=tmp_path)
    process = SimpleNamespace(pid=123)
    failure = OSError(errno.EIO, f"post-open {stage} failure")

    def identify(pid):
        if stage == "identity" and provider.pidfd_open.called:
            raise failure
        return "child"

    monkeypatch.setattr(groups, "process_birth_identity", lambda pid=None: "actor" if pid is None else identify(pid))
    monkeypatch.setattr(groups, "held_child_birth_identity", identify)
    monkeypatch.setattr(custody.os, "set_inheritable", Mock(side_effect=failure if stage == "inheritability" else None))
    closed = Mock()
    monkeypatch.setattr(custody.os, "close", closed)
    with pytest.raises(OSError) as error:
        if entry == "register":
            owner.register(process.pid, "child")
        else:
            owner.bind_launch(process)
    assert error.value is failure
    provider.pidfd_open.assert_called_once_with(123)
    provider.pidfd_send_signal.assert_not_called()
    assert not owner._handles
    closed.assert_called_once_with(73)
    owner.release_pidfds()
    closed.assert_called_once_with(73)
    if entry == "bind-launch":
        assert owner._uncertain is failure
        assert process._astrid_tree_uncertain == str(failure)
    else:
        assert owner._uncertain is None


@pytest.mark.integration
@pytest.mark.linux_native
def test_pack_host_and_retained_custody_without_stdlib_pidfds(tmp_path, without_stdlib_pidfds):
    from astrid.core.execution.generic_host import GenericPackHost

    host = GenericPackHost(pack_roots=[], attempt_root=tmp_path / "attempts")
    owner = custody.ProcessCustody(scope_root=tmp_path / "custody")
    argv = [sys.executable, "-c", "import time; time.sleep(60)"]
    wrapped, environment = owner.prepare_launch(argv, start_new_session=True)
    assert wrapped == argv and environment == {}
    child = subprocess.Popen(wrapped, start_new_session=True)
    try:
        owner.bind_launch(child)
        birth, descriptor = owner._handles[child.pid]
        assert not os.get_inheritable(descriptor)
        owner.signal(child.pid, birth, 0)
        assert child.poll() is None
        with pytest.raises(custody.CustodyError, match="retained exact process"):
            owner.signal(child.pid, "replacement", platform.TERM)
        assert child.poll() is None
        owner.signal(child.pid, birth, platform.TERM)
        assert child.wait(timeout=5) == -platform.TERM
        owner.release_pidfds(only_exited=True)
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        owner.release_pidfds()
        host.shutdown()


# --- sandbox process control: pidfd_open answers ENOSYS -----------------------

def test_strict_custody_never_falls_back_when_pidfd_open_is_missing(tmp_path, monkeypatch, runtime_pidfd_provider):
    runtime_pidfd_provider.pidfd_open.side_effect = OSError(errno.ENOSYS, "pidfd_open failed")
    monkeypatch.setattr(platform, "_process_control", "strict")
    monkeypatch.setattr(groups, "held_child_birth_identity", lambda _pid: "boot:5")
    monkeypatch.setattr(groups, "process_birth_identity", lambda pid=None: "actor" if pid is None else "boot:5")
    owner = custody.ProcessCustody(scope_root=tmp_path)
    with pytest.raises(OSError) as error:
        owner.bind_launch(SimpleNamespace(pid=123))
    assert error.value.errno == errno.ENOSYS and not owner._handles


@pytest.mark.linux_native
def test_sandbox_held_launch_is_signalled_through_its_popen(tmp_path, monkeypatch, sandbox_mode):
    monkeypatch.setattr(groups, "held_child_birth_identity", lambda _pid: "boot:5")
    monkeypatch.setattr(groups, "process_birth_identity", lambda pid=None: "actor" if pid is None else "boot:5")
    monkeypatch.setattr(platform, "signal_process", lambda *_: pytest.fail("held launch signalled by PID"))
    process = SimpleNamespace(pid=123, send_signal=Mock(), poll=Mock(return_value=None))
    owner = custody.ProcessCustody(scope_root=tmp_path)
    owner.bind_launch(process)
    birth, target = owner._handles[123]
    assert birth == "boot:5" and isinstance(target, custody._CheckedPidTarget) and target.identity is None
    owner.signal(123, "boot:5", platform.TERM)
    process.send_signal.assert_called_once_with(platform.TERM)
    owner.release_pidfds(only_exited=True)
    assert owner._handles[123][1] is not custody._PIDFD_RELEASED  # still running: still the obligation
    process.poll.return_value = 0
    owner.release_pidfds(only_exited=True)
    assert owner._handles[123][1] is custody._PIDFD_RELEASED
    owner.signal(123, "boot:5", platform.KILL)  # released: a no-op
    process.send_signal.assert_called_once()


@pytest.mark.linux_native
def test_sandbox_descendant_binds_runtime_identity_and_signals_through_runtime(tmp_path, monkeypatch, sandbox_mode):
    identity = {"pid": 456, "uid": 1000, "birth_id": "proc-start-ticks:77", "process_group": 123}
    monkeypatch.setattr(platform, "process_identity", lambda pid: dict(identity) if pid == 456 else None)
    monkeypatch.setattr(groups, "process_birth_identity", lambda pid=None: "actor" if pid is None else "boot:77")
    sent = []
    monkeypatch.setattr(platform, "signal_process", lambda expected, sig: sent.append((expected, sig)))
    owner = custody.ProcessCustody(scope_root=tmp_path)
    owner._actor_birth = "actor"
    owner.register(456, "boot:77")
    target = owner._handles[456][1]
    assert isinstance(target, custody._CheckedPidTarget) and target.identity == identity
    owner.signal(456, "boot:77", platform.TERM)
    assert sent == [(identity, platform.TERM)]


@pytest.mark.linux_native
def test_sandbox_descendant_with_another_start_time_is_not_admitted(tmp_path, monkeypatch, sandbox_mode):
    monkeypatch.setattr(platform, "process_identity",
                        lambda pid: {"pid": pid, "uid": 1000, "birth_id": "proc-start-ticks:99"})
    monkeypatch.setattr(groups, "process_birth_identity", lambda pid=None: "actor" if pid is None else "boot:77")
    owner = custody.ProcessCustody(scope_root=tmp_path)
    owner.register(456, "boot:77")
    assert 456 not in owner._handles


@pytest.mark.linux_native
def test_sandbox_signal_to_an_exited_descendant_is_not_an_error(tmp_path, monkeypatch, sandbox_mode):
    identity = {"pid": 456, "uid": 1000, "birth_id": "proc-start-ticks:77"}
    current = {"value": dict(identity)}
    monkeypatch.setattr(platform, "process_identity", lambda pid: current["value"])
    monkeypatch.setattr(groups, "process_birth_identity", lambda pid=None: "actor" if pid is None else "boot:77")
    owner = custody.ProcessCustody(scope_root=tmp_path)
    owner._actor_birth = "actor"
    owner.register(456, "boot:77")

    def changed(*_):
        raise OSError("process incarnation changed")

    monkeypatch.setattr(platform, "signal_process", changed)
    owner.signal(456, "boot:77", platform.TERM)  # exited (pid maybe reused): nothing of ours to signal
    current["value"] = {"pid": 456, "uid": 1000, "birth_id": "proc-start-ticks:500"}
    owner.release_pidfds(only_exited=True)
    assert owner._handles[456][1] is custody._PIDFD_RELEASED

    def denied(*_):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    owner._handles[456] = ("boot:77", custody._CheckedPidTarget(identity))
    monkeypatch.setattr(platform, "signal_process", denied)
    with pytest.raises(custody.CustodyError, match="start-time-checked signal failed"):
        owner.signal(456, "boot:77", platform.TERM)


@pytest.mark.linux_native
def test_sandbox_custody_stops_a_real_tree_without_pidfds(tmp_path, monkeypatch, sandbox_mode):
    """End to end in sandbox mode: the launch and a real descendant, no pidfd anywhere."""
    owner = custody.ProcessCustody(scope_root=tmp_path)
    marker = tmp_path / "child.pid"
    code = ("import subprocess,sys,pathlib,time; "
            "c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(c.pid)); time.sleep(60)")
    argv, _ = owner.prepare_launch([sys.executable, "-c", code, str(marker)], start_new_session=True)
    root = subprocess.Popen(argv, start_new_session=True)
    try:
        owner.bind_launch(root)
        import time
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        child = int(marker.read_text())
        child_birth = groups.process_birth_identity(child)
        owner.register(child, child_birth)
        assert isinstance(owner._handles[child][1], custody._CheckedPidTarget)
        owner.signal(child, child_birth, platform.KILL)
        owner.signal(root.pid, owner._handles[root.pid][0], platform.KILL)
        assert root.wait(timeout=10) == -platform.KILL
        deadline = time.monotonic() + 10
        while platform.process_identity(child) is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert platform.process_identity(child) is None
        owner.release_pidfds(only_exited=True)
        assert all(handle is custody._PIDFD_RELEASED for _, handle in owner._handles.values())
    finally:
        if root.poll() is None:
            root.kill()
            root.wait(timeout=5)
