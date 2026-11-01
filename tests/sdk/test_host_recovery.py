"""A pack host is acted on by its registration, whichever process launched it."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core._shared.boot_manifest import load_boot_manifest_hash
from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import (
    source_checkout_closure_digest,
    source_checkout_digest,
)
from astrid.sdk import host_bootstrap
from tests.sdk.test_host_bootstrap_source_identity import _materialize_source_closure

FAKE_PID = 4242
MODULE_ARGS = ["-m", "astrid.core.execution.generic_host", "run", "--support-root"]
# A leader that optionally keeps one child running, then idles.
LEADER = ("import subprocess,sys,time;"
          "c=subprocess.Popen([sys.executable,'-c','import sys;sys.stdin.buffer.read()'],stdin=sys.stdin,start_new_session=sys.argv[1]=='task') if sys.argv[1] in ('child','task') else None;"
          "open(sys.argv[2],'w').write(str(c.pid if c else 0));"
          "time.sleep(300 if sys.argv[3]=='stay' else 0)")


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.tmp, self.mp = tmp_path, monkeypatch
        self.source = tmp_path / "source"
        (self.source / "astrid" / "packs").mkdir(parents=True)
        _materialize_source_closure(self.source)
        self.support = tmp_path / "support"
        (self.support / "credentials").mkdir(parents=True)
        self.credential = self.support / "credentials" / "worker.token"
        self.credential.write_text("worker", encoding="utf-8")
        os.chmod(self.credential, 0o600)
        self.value = {
            "worker_credential_file": str(self.credential), "source_checkout": str(self.source),
            "worker_actor": host_bootstrap.PACK_HOST_ACTOR, "worker_scopes": list(host_bootstrap.PACK_HOST_SCOPES),
            "endpoint": "http://runtime.test", "runtime_epoch": "epoch-1",
            "runtime_instance_id": "instance-1", "schema_digest": "schema-1",
        }
        self.launches: list[dict] = []
        self.procs: list[subprocess.Popen] = []
        inventory = SimpleNamespace(identity="", roots=(), sources=())
        monkeypatch.setattr("astrid.core.pack.source_setup.active_source_inventory", lambda: inventory)

        class Client:
            def __init__(self, *_a): pass
            def health(self):
                return {"status": "ok", "runtime_epoch": "epoch-1", "runtime_instance_id": "instance-1",
                        "schema_digest": "schema-1"}
            def _authenticate_worker(self, _executor_id):
                return "realm-1"
            @property
            def generated(self):
                return SimpleNamespace(get_executor=lambda _executor_id: SimpleNamespace(
                    executor_id=host_bootstrap.PACK_HOST_ACTOR, readiness="ready",
                    runtime_epoch="epoch-1", source_epoch="source-epoch-1",
                    source_digest="source-digest-1", dependency_digest="dependency-digest-1",
                    capabilities=[SimpleNamespace(status="ready")],
                ))
        monkeypatch.setattr(generic_host, "RuntimeProtocolClient", Client)
        real_birth = host_bootstrap._host_birth_identity
        monkeypatch.setattr(host_bootstrap, "_host_birth_identity",
                            lambda pid: "birth-1" if pid == FAKE_PID else real_birth(pid))
        monkeypatch.setattr(host_bootstrap, "popen_owned_group", self._popen)
        monkeypatch.setattr(host_bootstrap, "_IDLE_CHECK_INTERVAL_SECONDS", 0.0)
        monkeypatch.setattr(host_bootstrap, "_GROUP_GRACE_SECONDS", 2.0)

    def _popen(self, argv, **kwargs):
        self.launches.append({"argv": list(argv), "env": kwargs["env"]})
        manifest = self.support / "astrid-host" / "boot-manifest.json"
        (self.support / "generic-host.ready.json").write_text(json.dumps({
            "status": "ready", "pid": FAKE_PID, "process_birth_id": "birth-1",
            "python_executable": host_bootstrap._pack_host_python_executable(),
            "endpoint": "http://runtime.test", "executor_id": host_bootstrap.PACK_HOST_ACTOR,
            "ready_file": str(self.support / "generic-host.ready.json"),
            "credential_file": str(self.credential), "support_root": str(self.support),
            "source_checkout": str(self.source), "source_checkout_digest": source_checkout_digest(self.source),
            "source_closure_digest": source_checkout_closure_digest(self.source),
            "source_inventory_identity": "",
            "boot_manifest_path": str(manifest),
            "boot_manifest_hash": load_boot_manifest_hash(manifest, support_root=self.support),
            "runtime_instance_id": "instance-1", "runtime_epoch": "epoch-1", "schema_digest": "schema-1",
            "source_epoch": "source-epoch-1",
            "registration": {
                "source_epoch": "source-epoch-1", "source_digest": "source-digest-1",
                "dependency_digest": "dependency-digest-1",
            },
            "ready_capabilities": [],
        }), encoding="utf-8")
        return SimpleNamespace(pid=FAKE_PID, poll=lambda: None)

    def ensure(self):
        return host_bootstrap.ensure_pack_host(self.value, reconfigure_action="reconfigure")

    def marker(self, name="generic-host.json") -> dict:
        return json.loads((self.support / name).read_text(encoding="utf-8"))

    def register(self, pid: int, birth: str | None = None, **changes) -> None:
        """Point the markers a first (fake) launch wrote at *pid*."""
        for name in ("generic-host.json", "generic-host.ready.json"):
            data = self.marker(name)
            data.update(pid=pid, process_birth_id=birth or host_bootstrap._host_birth_identity(pid))
            if name == "generic-host.json":
                data.update(changes)
            (self.support / name).write_text(json.dumps(data), encoding="utf-8")

    def host(self, child: str = "none", stay: bool = True) -> tuple[subprocess.Popen, int]:
        """A real process leading its own session that looks like a pack host."""
        record = self.tmp / f"child-{len(self.procs)}.pid"
        proc = subprocess.Popen([sys.executable, "-c", LEADER, child, str(record), "stay" if stay else "go",
                                 *MODULE_ARGS, str(self.support)], start_new_session=True, stdin=subprocess.PIPE)
        self.procs.append(proc)
        deadline = time.monotonic() + 10
        while not record.exists() or not record.read_text():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        return proc, int(record.read_text())

    def kill_leader(self, proc: subprocess.Popen) -> None:
        """Register the live leader (so its start time is recorded), then kill only it."""
        self.register(proc.pid)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()

    def cleanup(self) -> None:
        for proc in self.procs:
            if proc.stdin is not None:
                proc.stdin.close()
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()


@pytest.fixture
def harness(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.ensure()  # a first launch writes the markers every case starts from
    h.launches.clear()
    yield h
    h.cleanup()


def test_launcher_attaches_to_healthy_host_from_another_checkout(harness, monkeypatch, tmp_path):
    h = harness
    second = tmp_path / "second-source"
    (second / "astrid" / "packs").mkdir(parents=True)
    _materialize_source_closure(second)
    h.value["source_checkout"] = str(second)
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "ours")
    from astrid.core.gateway import dispatch
    monkeypatch.setattr(
        dispatch, "compose_profile_handoff",
        lambda *_args, **_kwargs: pytest.fail("attach must preserve the resident boot manifest"),
    )

    result = host_bootstrap.ensure_pack_host(
        h.value, reconfigure_action="reconfigure", attach_healthy=True,
    )

    assert result["host_status"] == "ready"
    assert result["host_pid"] == FAKE_PID
    assert result["host_source_checkout_digest"] == source_checkout_digest(h.source)
    assert h.launches == []


@pytest.mark.parametrize("fault", [
    "unready-registration", "changed-registration-generation", "changed-resident-source",
])
def test_launcher_replaces_cross_checkout_host_when_resident_is_unhealthy(
    harness, monkeypatch, tmp_path, fault,
):
    h = harness
    resident_source = h.source
    second = tmp_path / "second-source"
    (second / "astrid" / "packs").mkdir(parents=True)
    _materialize_source_closure(second)
    h.value["source_checkout"] = str(second)
    h.source = second
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "ours")
    monkeypatch.setattr(host_bootstrap, "_host_idle", lambda *_args: True)
    retired = []
    monkeypatch.setattr(host_bootstrap, "_retire_host_locked", lambda state: retired.append(dict(state)))

    if fault == "unready-registration":
        client_type = generic_host.RuntimeProtocolClient

        class UnreadyClient(client_type):
            @property
            def generated(self):
                return SimpleNamespace(get_executor=lambda _executor_id: SimpleNamespace(
                    executor_id=host_bootstrap.PACK_HOST_ACTOR, readiness="starting",
                    runtime_epoch="epoch-1", source_epoch="source-epoch-1",
                    source_digest="source-digest-1", dependency_digest="dependency-digest-1",
                    capabilities=[SimpleNamespace(status="ready")],
                ))

        monkeypatch.setattr(generic_host, "RuntimeProtocolClient", UnreadyClient)
    elif fault == "changed-registration-generation":
        client_type = generic_host.RuntimeProtocolClient

        class SupersededClient(client_type):
            @property
            def generated(self):
                return SimpleNamespace(get_executor=lambda _executor_id: SimpleNamespace(
                    executor_id=host_bootstrap.PACK_HOST_ACTOR, readiness="ready",
                    runtime_epoch="epoch-1", source_epoch="source-epoch-2",
                    source_digest="source-digest-1", dependency_digest="dependency-digest-1",
                    capabilities=[SimpleNamespace(status="ready")],
                ))

        monkeypatch.setattr(generic_host, "RuntimeProtocolClient", SupersededClient)
    else:
        (resident_source / "astrid" / "packs" / "changed.marker").write_text("changed")
    result = host_bootstrap.ensure_pack_host(
        h.value, reconfigure_action="reconfigure", attach_healthy=True,
    )

    assert result["host_status"] == "ready"
    assert len(retired) == 1
    assert len(h.launches) == 1


def _defunct(pid: int) -> bool:
    """True for a terminated process whose parent has not reaped it yet."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[:1] == ["Z"]


def _gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        if _defunct(pid):
            return True  # killed, awaiting a reaper (a container's PID 1 may reap late)
        time.sleep(0.05)
    return False


def test_gone_host_with_empty_group_is_dropped_and_relaunched(harness):
    proc, _ = harness.host()
    harness.kill_leader(proc)
    harness.ensure()
    assert len(harness.launches) == 1
    assert harness.marker()["pid"] == FAKE_PID


def test_dead_leader_with_surviving_member_is_killed_then_dropped(harness):
    proc, member = harness.host(child="child")
    harness.kill_leader(proc)
    assert not _gone(member, 0.2), "the orphan must be alive before recovery"
    harness.ensure()
    assert _gone(member), "orphan in the dead host's group must be killed"
    assert len(harness.launches) == 1 and harness.marker()["pid"] == FAKE_PID


def test_live_host_with_stale_source_is_retired_by_registration_when_idle(harness):
    proc, _ = harness.host()
    harness.register(proc.pid, source_closure_digest="stale")
    harness.ensure()
    assert proc.wait(timeout=10) is not None
    assert len(harness.launches) == 1 and harness.marker()["pid"] == FAKE_PID


def test_node_discovery_exception_leaves_host_running_and_render_unconfigured(harness, monkeypatch, caplog):
    project = harness.source / "remotion"
    (project / "node_modules").mkdir(parents=True)
    (project / "package.json").write_text("{}", encoding="utf-8")
    (harness.source / "astrid" / "core" / "execution" / "generic_host.py").write_text(
        "# source changed to require a host replacement\n", encoding="utf-8"
    )
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "ours")
    monkeypatch.setattr(host_bootstrap, "_host_idle", lambda *_args: True)
    retired = []
    monkeypatch.setattr(host_bootstrap, "_retire_host_locked", lambda state: retired.append(dict(state)))

    def fail_node_discovery(*_args, **_kwargs):
        raise RuntimeError("Node discovery unavailable")

    monkeypatch.setattr("astrid.core.rendering.node_pin.resolve_pinned_node", fail_node_discovery)
    harness.ensure()

    assert len(harness.launches) == 1
    assert len(retired) == 1
    child_env = harness.launches[0]["env"]
    assert "ASTRID_NODE_EXECUTABLE" not in child_env
    assert "Node discovery unavailable" in caplog.text


def test_busy_prior_host_is_not_replaced(harness, monkeypatch):
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "ours")
    monkeypatch.setattr(host_bootstrap, "_host_idle", lambda *_args: False)
    retired = []
    monkeypatch.setattr(host_bootstrap, "_retire_host_locked", lambda state: retired.append(dict(state)))

    (harness.source / "astrid" / "core" / "execution" / "generic_host.py").write_text(
        "# source changed to require a host replacement\n", encoding="utf-8"
    )
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="busy"):
        harness.ensure()

    assert not harness.launches and not retired


def test_busy_live_host_is_deferred_and_untouched(harness):
    proc, child = harness.host(child="task")  # task work runs in its own session
    harness.register(proc.pid, source_closure_digest="stale")
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="busy"):
        harness.ensure()
    assert proc.poll() is None and not _gone(child, 0.2)
    assert harness.marker()["pid"] == proc.pid and not harness.launches


def test_live_matching_host_is_reused(harness):
    proc, _ = harness.host()
    harness.register(proc.pid)
    assert harness.ensure()["host_pid"] == proc.pid
    assert not harness.launches and proc.poll() is None


def test_spawn_env_excludes_job_lease(harness, monkeypatch):
    monkeypatch.setenv("OTTO_HEAVY_LEASE", "lease-123")
    monkeypatch.setenv("KEEP_ME", "1")
    harness.register(harness.host()[0].pid, source_closure_digest="stale")
    harness.ensure()
    env = harness.launches[0]["env"]
    assert "OTTO_HEAVY_LEASE" not in env and env["KEEP_ME"] == "1"


def test_pid_reused_by_unrelated_process_is_never_signalled(harness):
    proc, child = harness.host(child="child")
    harness.register(proc.pid, birth="not-its-start-time")
    harness.ensure()
    assert proc.poll() is None and not _gone(child, 0.2)
    assert len(harness.launches) == 1 and harness.marker()["pid"] == FAKE_PID
    with pytest.raises(host_bootstrap.PackHostBootstrapError):
        host_bootstrap._stop_group(os.getpgrp())  # never our own group



def test_host_helper_in_its_own_group_is_not_work(harness):
    # The per-claim git preflight runs in the host's own process group: not a task.
    proc, helper = harness.host(child="child")
    harness.register(proc.pid, source_closure_digest="stale")
    harness.ensure()
    assert proc.wait(timeout=10) is not None and len(harness.launches) == 1


def test_published_busy_record_decides(harness):
    proc, _ = harness.host()
    harness.register(proc.pid, source_closure_digest="stale")
    busy = harness.support / "generic-host.busy.json"
    busy.write_text(json.dumps({"pid": proc.pid, "active": 1}), encoding="utf-8")
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="busy"):
        harness.ensure()
    assert proc.poll() is None
    busy.write_text(json.dumps({"pid": proc.pid, "active": 0}), encoding="utf-8")
    harness.ensure()
    assert proc.wait(timeout=10) is not None and len(harness.launches) == 1


def test_host_publishes_its_in_flight_task_count(tmp_path):
    from astrid.core.execution import generic_host
    path = tmp_path / generic_host.BUSY_NAME
    generic_host._publish_busy(path, +1)
    assert json.loads(path.read_text())["active"] >= 1 and json.loads(path.read_text())["pid"] == os.getpid()
    generic_host._publish_busy(path, -1)
    assert json.loads(path.read_text())["active"] == 0
    assert oct(path.stat().st_mode & 0o777) == oct(0o600)



@pytest.mark.posix_native  # POSIX-only process-group API
def test_stop_group_signals_anchored_group_and_refuses_a_reused_leader(owned_process_groups):
    from astrid.core.execution.process_group import process_birth_identity
    code = ("import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(300)']);time.sleep(300)")
    proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    owned_process_groups.record(proc)
    try:
        deadline = time.monotonic() + 15
        while len(host_bootstrap._group_survivors(proc.pid)) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        birth = process_birth_identity(proc.pid)
        assert birth and len(host_bootstrap._group_survivors(proc.pid)) == 2
        with pytest.raises(host_bootstrap.PackHostBootstrapError, match="reused"):
            host_bootstrap._stop_group(proc.pid, leader_birth=birth + "x")
        assert proc.poll() is None
        host_bootstrap._stop_group(proc.pid, grace=2.0, leader_birth=birth)
        proc.wait(timeout=10)
        assert not host_bootstrap._group_survivors(proc.pid)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_stop_group_signals_only_the_registered_anchored_group(monkeypatch):
    calls = []
    census = {
        4321: SimpleNamespace(pid=4321, pgid=4321, birth="birth-current"),
        4322: SimpleNamespace(pid=4322, pgid=4321, birth="child-birth"),
    }
    monkeypatch.setattr(host_bootstrap, "_process_snapshot", lambda: census)
    monkeypatch.setattr(host_bootstrap, "_host_pid_alive", lambda _pid: True)
    monkeypatch.setattr(host_bootstrap.os, "getpid", lambda: 9999)
    monkeypatch.setattr(host_bootstrap.platform, "process_group", lambda _pid: 8765)
    def signal_group(pgid, signum, *, leader_birth):
        assert leader_birth == "birth-current"
        calls.append((pgid, signum))
        census.clear()
    monkeypatch.setattr(host_bootstrap, "signal_observed_group", signal_group)

    assert host_bootstrap._stop_group(4321, grace=0, leader_birth="birth-current") is False
    assert calls == [(4321, host_bootstrap.platform.TERM)]

    calls.clear()
    census.update({
        4321: SimpleNamespace(pid=4321, pgid=4321, birth="replacement-birth"),
        4322: SimpleNamespace(pid=4322, pgid=4321, birth="child-birth"),
    })
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="reused"):
        host_bootstrap._stop_group(4321, grace=0, leader_birth="birth-current")
    assert calls == []

    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="unsafe"):
        host_bootstrap._stop_group(8765, grace=0, leader_birth="birth-current")
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="lacks a leader birth anchor"):
        host_bootstrap._stop_group(4321, grace=0)
    assert calls == []


def test_stop_group_rechecks_leader_birth_before_escalation(monkeypatch):
    leader = SimpleNamespace(pid=4321, pgid=4321, birth="registered-birth")
    calls = []
    monkeypatch.setattr(host_bootstrap, "_process_snapshot", lambda: {4321: leader})
    monkeypatch.setattr(host_bootstrap, "_host_pid_alive", lambda _pid: True)
    monkeypatch.setattr(host_bootstrap.os, "getpid", lambda: 9999)
    monkeypatch.setattr(host_bootstrap.platform, "process_group", lambda _pid: 8765)
    def signal_group(pgid, signum, *, leader_birth):
        assert leader_birth == "registered-birth"
        calls.append((pgid, signum))
        leader.birth = "replacement-birth"
    monkeypatch.setattr(host_bootstrap, "signal_observed_group", signal_group)

    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="reused"):
        host_bootstrap._stop_group(4321, grace=0, leader_birth="registered-birth")
    assert calls == [(4321, host_bootstrap.platform.TERM)]


def test_stop_group_reports_sigkill_escalation_without_live_processes(monkeypatch):
    state = {"alive": True}
    calls = []

    def survivors(_pgid, *, leader_birth=None):
        if leader_birth is not None:
            assert leader_birth == "birth-current"
        return [4321] if state["alive"] else []

    def signal_group(pgid, signum, *, leader_birth):
        assert leader_birth == "birth-current"
        calls.append((pgid, signum))
        if signum == host_bootstrap.platform.KILL:
            state["alive"] = False

    monkeypatch.setattr(host_bootstrap, "_group_survivors", survivors)
    monkeypatch.setattr(host_bootstrap.os, "getpid", lambda: 9999)
    monkeypatch.setattr(host_bootstrap.platform, "process_group", lambda _pid: 8765)
    monkeypatch.setattr(host_bootstrap, "signal_observed_group", signal_group)

    forced = host_bootstrap._stop_group(4321, grace=0, leader_birth="birth-current")

    assert forced is True
    assert calls == [(4321, host_bootstrap.platform.TERM), (4321, host_bootstrap.platform.KILL)]


def test_registered_windows_host_uses_kernel_identity_without_posix_census(monkeypatch):
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    state = {
        "pid": 4321, "process_birth_id": "windows-creation:1234",
        "support_root": r"C:\Astrid\support\runtime",
        "python_executable": r"C:\Astrid\venv\Scripts\python.exe",
    }
    monkeypatch.setattr(host_bootstrap.platform, "process_identity", lambda pid: {
        "pid": pid, "birth_id": "windows-creation:1234", "uid": "sid",
    })
    monkeypatch.setattr(host_bootstrap.platform, "process_executable", lambda _pid: r"c:\astrid\VENV\Scripts\Python.EXE")
    monkeypatch.setattr(host_bootstrap, "_windows_process_argv", lambda _pid: [
        r"C:\Astrid\venv\Scripts\python.exe", "-X", "utf8", "-m", host_bootstrap._HOST_MODULE, "run",
        "--support-root", state["support_root"],
    ])
    monkeypatch.setattr(host_bootstrap, "_process_snapshot", lambda: pytest.fail("Windows used POSIX census"))
    monkeypatch.setattr(host_bootstrap, "_ps_command", lambda _pid: pytest.fail("Windows ran ps"))

    assert host_bootstrap._registered_host_status(state) == "ours"


@pytest.mark.parametrize("observed,expected", [
    (None, "gone"),
    ({"birth_id": "windows-creation:9999"}, "reused"),
])
def test_registered_windows_host_distinguishes_gone_and_reused(monkeypatch, observed, expected):
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(host_bootstrap.platform, "process_identity", lambda _pid: observed)
    monkeypatch.setattr(host_bootstrap, "_process_snapshot", lambda: pytest.fail("Windows used POSIX census"))

    state = {"pid": 4321, "process_birth_id": "windows-creation:1234"}
    assert host_bootstrap._registered_host_status(state) == expected


def test_registered_windows_host_fails_closed_when_identity_is_denied(monkeypatch):
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    def denied(_pid):
        raise PermissionError("access denied")
    monkeypatch.setattr(host_bootstrap.platform, "process_identity", denied)
    monkeypatch.setattr(host_bootstrap, "_process_snapshot", lambda: pytest.fail("Windows used POSIX census"))

    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="native process identity unavailable"):
        host_bootstrap._registered_host_status({"pid": 4321, "process_birth_id": "windows-creation:1234"})


def test_registered_windows_host_rechecks_identity_after_executable_observation(monkeypatch):
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    identities = iter((
        {"pid": 4321, "birth_id": "windows-creation:1234"},
        {"pid": 4321, "birth_id": "windows-creation:9999"},
    ))
    monkeypatch.setattr(host_bootstrap.platform, "process_identity", lambda _pid: next(identities))
    monkeypatch.setattr(host_bootstrap.platform, "process_executable", lambda _pid: r"C:\Astrid\venv\Scripts\python.exe")
    monkeypatch.setattr(host_bootstrap, "_windows_process_argv", lambda _pid: [
        r"C:\Astrid\venv\Scripts\python.exe", "-X", "utf8", "-m", host_bootstrap._HOST_MODULE, "run",
        "--support-root", r"C:\Astrid\support",
    ])

    state = {"pid": 4321, "process_birth_id": "windows-creation:1234",
             "support_root": r"C:\Astrid\support", "python_executable": r"C:\Astrid\venv\Scripts\python.exe"}
    assert host_bootstrap._registered_host_status(state) == "reused"


def test_windows_registered_host_command_must_bind_module_and_support_root(monkeypatch):
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(host_bootstrap.platform, "process_identity", lambda pid: {
        "pid": pid, "birth_id": "windows-creation:1234",
    })
    monkeypatch.setattr(host_bootstrap.platform, "process_executable", lambda _pid: r"C:\Astrid\venv\Scripts\python.exe")
    monkeypatch.setattr(host_bootstrap, "_windows_process_argv", lambda _pid: [
        r"C:\Astrid\venv\Scripts\python.exe", "-X", "utf8", "-m", "unrelated.module", "run",
        "--support-root", r"C:\Astrid\other-support",
    ])
    state = {"pid": 4321, "process_birth_id": "windows-creation:1234",
             "support_root": r"C:\Astrid\support", "python_executable": r"C:\Astrid\venv\Scripts\python.exe"}

    assert host_bootstrap._registered_host_status(state) == "foreign"


def test_windows_registered_host_binds_launcher_path_and_captured_kernel_image_separately(monkeypatch):
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(host_bootstrap.platform, "process_identity", lambda pid: {
        "pid": pid, "birth_id": "windows-creation:1234",
    })
    monkeypatch.setattr(host_bootstrap.platform, "process_executable", lambda _pid: r"C:\Python311\python.exe")
    monkeypatch.setattr(host_bootstrap, "_windows_process_argv", lambda _pid: [
        r"C:\Python311\python.exe", "-X", "utf8", "-m", host_bootstrap._HOST_MODULE, "run",
        "--support-root", r"C:\Astrid\support",
    ])
    state = {"pid": 4321, "process_birth_id": "windows-creation:1234",
             "support_root": r"C:\Astrid\support", "python_executable": r"C:\Astrid\venv\Scripts\python.exe",
             "process_image": r"C:\Python311\python.exe"}

    assert host_bootstrap._registered_host_status(state) == "ours"
    assert host_bootstrap._registered_host_status({**state, "process_image": r"C:\Other\python.exe"}) == "foreign"


@pytest.mark.windows_native
def test_legacy_windows_venv_registration_accepts_base_interpreter_image(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    scripts = venv / "Scripts"
    scripts.mkdir(parents=True)
    base = tmp_path / "base"
    base.mkdir()
    (base / "python.exe").write_bytes(b"test executable placeholder")
    (venv / "pyvenv.cfg").write_text(f"home = {base}\n", encoding="utf-8")
    selected = scripts / "python.exe"
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(host_bootstrap.platform, "process_identity", lambda pid: {
        "pid": pid, "birth_id": "windows-creation:1234",
    })
    monkeypatch.setattr(host_bootstrap.platform, "process_executable", lambda _pid: str(base / "python.exe"))
    monkeypatch.setattr(host_bootstrap, "_windows_process_argv", lambda _pid: [
        str(base / "python.exe"), "-X", "utf8", "-m", host_bootstrap._HOST_MODULE, "run",
        "--support-root", r"C:\Astrid\support",
    ])

    state = {
        "pid": 4321,
        "process_birth_id": "windows-creation:1234",
        "support_root": r"C:\Astrid\support",
        "python_executable": str(selected),
    }
    assert host_bootstrap._windows_venv_base_executable(str(selected)) == str(base / "python.exe")
    assert host_bootstrap._registered_host_status(state) == "ours"


def test_registered_windows_host_fails_closed_when_command_observation_is_denied(monkeypatch):
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(host_bootstrap.platform, "process_identity", lambda pid: {
        "pid": pid, "birth_id": "windows-creation:1234",
    })
    monkeypatch.setattr(host_bootstrap.platform, "process_executable", lambda _pid: r"C:\Astrid\venv\Scripts\python.exe")
    def denied(_pid):
        raise OSError("access denied")
    monkeypatch.setattr(host_bootstrap, "_windows_process_argv", denied)
    state = {"pid": 4321, "process_birth_id": "windows-creation:1234",
             "support_root": r"C:\Astrid\support", "python_executable": r"C:\Astrid\venv\Scripts\python.exe"}

    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="native process metadata unavailable"):
        host_bootstrap._registered_host_status(state)


def test_windows_live_host_retirement_requires_retained_job(monkeypatch):
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "gone")

    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="no retained Job custody"):
        host_bootstrap._terminate_old_host({"pid": 4321, "process_birth_id": "windows-creation:1234"})


def test_windows_gone_host_without_retained_job_keeps_registration_markers(monkeypatch, tmp_path):
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "gone")
    state = {"pid": 4321, "process_birth_id": "windows-creation:1234", "support_root": str(tmp_path)}
    markers = [tmp_path / "generic-host.json", tmp_path / "generic-host.ready.json"]
    for marker in markers:
        marker.write_text("{}", encoding="utf-8")

    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="no retained Job custody"):
        host_bootstrap._retire_host_locked(state)
    assert all(marker.exists() for marker in markers)


def test_windows_supervisor_receipt_proves_only_the_bound_drained_service_job(monkeypatch, tmp_path):
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(host_bootstrap.platform, "is_owner_only", lambda _path: True)
    monkeypatch.setattr(host_bootstrap.platform, "owner_only", lambda _path: None)
    monkeypatch.setattr(host_bootstrap.platform, "lock_file", lambda _fd: None)
    monkeypatch.setenv("ASTRID_RUNTIME_SERVICE_NAME", "pack-host")
    support_root = str(tmp_path)
    record = {
        "version": 1, "service_name": "pack-host", "support_root": support_root,
        "receipts": [{"pid": 4321, "process_birth_id": "windows-creation:1234", "job_drained": True}],
    }
    (tmp_path / "pack-host-job-cleanup.json").write_text(json.dumps(record), encoding="utf-8")
    state = {"pid": 4321, "process_birth_id": "windows-creation:1234", "support_root": support_root}

    assert host_bootstrap._runtime_cleanup_receipt_matches(state, foreground=True)
    assert not host_bootstrap._runtime_cleanup_receipt_matches(state, foreground=False)
    assert not host_bootstrap._runtime_cleanup_receipt_matches({**state, "pid": 4322}, foreground=True)
    assert not host_bootstrap._runtime_cleanup_receipt_matches(
        {**state, "process_birth_id": "windows-creation:other"}, foreground=True
    )
    record["receipts"][0]["job_drained"] = False
    (tmp_path / "pack-host-job-cleanup.json").write_text(json.dumps(record), encoding="utf-8")
    assert not host_bootstrap._runtime_cleanup_receipt_matches(state, foreground=True)


def test_windows_supervisor_launch_id_bridges_venv_redirector_pid(monkeypatch, tmp_path):
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(host_bootstrap.platform, "is_owner_only", lambda _path: True)
    monkeypatch.setattr(host_bootstrap.platform, "owner_only", lambda _path: None)
    monkeypatch.setattr(host_bootstrap.platform, "lock_file", lambda _fd: None)
    monkeypatch.setenv("ASTRID_RUNTIME_SERVICE_NAME", "pack-host")
    support_root = str(tmp_path)
    launch_id = "a" * 32
    state = {"pid": 4321, "process_birth_id": "windows-creation:1234", "support_root": support_root,
             "runtime_service_launch_id": launch_id}
    (tmp_path / "pack-host-job-cleanup.json").write_text(json.dumps({
        "version": 1, "service_name": "pack-host", "support_root": support_root,
        "receipts": [{"pid": None, "process_birth_id": None,
                      "runtime_service_launch_id": launch_id, "job_drained": True}],
    }), encoding="utf-8")

    assert host_bootstrap._runtime_cleanup_receipt_matches(state, foreground=True)
    assert not host_bootstrap._runtime_cleanup_receipt_matches(
        {**state, "runtime_service_launch_id": "b" * 32}, foreground=True
    )


def test_runtime_service_launch_id_is_scoped_to_foreground_pack_host(monkeypatch):
    monkeypatch.setenv("ASTRID_RUNTIME_SERVICE_NAME", "pack-host")
    monkeypatch.setenv("ASTRID_RUNTIME_SERVICE_LAUNCH_ID", "c" * 32)
    assert host_bootstrap._runtime_service_launch_id(foreground=True) == "c" * 32
    assert host_bootstrap._runtime_service_launch_id(foreground=False) is None
    monkeypatch.setenv("ASTRID_RUNTIME_SERVICE_LAUNCH_ID", "malformed")
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="launch identity is malformed"):
        host_bootstrap._runtime_service_launch_id(foreground=True)


def test_windows_foreground_retry_uses_matching_receipt_without_signaling_pid(monkeypatch, tmp_path):
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(host_bootstrap.platform, "is_owner_only", lambda _path: True)
    monkeypatch.setattr(host_bootstrap.platform, "owner_only", lambda _path: None)
    monkeypatch.setattr(host_bootstrap.platform, "lock_file", lambda _fd: None)
    monkeypatch.setenv("ASTRID_RUNTIME_SERVICE_NAME", "pack-host")
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "reused")
    state = {"pid": 4321, "process_birth_id": "windows-creation:1234", "support_root": str(tmp_path)}
    (tmp_path / "pack-host-job-cleanup.json").write_text(json.dumps({
        "version": 1, "service_name": "pack-host", "support_root": str(tmp_path),
        "receipts": [{"pid": 4321, "process_birth_id": "windows-creation:1234", "job_drained": True}],
    }), encoding="utf-8")
    monkeypatch.setattr(host_bootstrap, "_stop_group", lambda *_args, **_kwargs: pytest.fail("signalled Windows PID"))

    assert host_bootstrap._terminate_old_host(state, foreground=True) is False


def test_windows_retirement_consumes_only_matching_supervisor_receipt(monkeypatch, tmp_path):
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(host_bootstrap.platform, "is_owner_only", lambda _path: True)
    monkeypatch.setattr(host_bootstrap.platform, "owner_only", lambda _path: None)
    monkeypatch.setattr(host_bootstrap.platform, "lock_file", lambda _fd: None)
    monkeypatch.setenv("ASTRID_RUNTIME_SERVICE_NAME", "pack-host")
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "gone")
    support_root = str(tmp_path)
    state = {"pid": 4321, "process_birth_id": "windows-creation:1234", "support_root": support_root}
    cleanup_path = tmp_path / "pack-host-job-cleanup.json"
    cleanup_path.write_text(json.dumps({
        "version": 1, "service_name": "pack-host", "support_root": support_root,
        "receipts": [
            {"pid": 4321, "process_birth_id": "windows-creation:1234", "job_drained": True},
            {"pid": 4444, "process_birth_id": "windows-creation:other", "job_drained": True},
        ],
    }), encoding="utf-8")
    ready = tmp_path / "generic-host.ready.json"
    registration = tmp_path / "generic-host.json"
    ready.write_text("{}", encoding="utf-8")
    registration.write_text("{}", encoding="utf-8")

    host_bootstrap._retire_host_locked(state, foreground=True)

    assert not ready.exists() and not registration.exists()
    assert json.loads(cleanup_path.read_text(encoding="utf-8"))["receipts"] == [
        {"pid": 4444, "process_birth_id": "windows-creation:other", "job_drained": True},
    ]


def test_windows_command_observation_uses_read_only_cim_and_validates_pid(monkeypatch):
    calls = []
    payload = json.dumps({
        "ProcessId": 4321,
        "CommandLine": 'python.exe -m astrid.core.execution.generic_host run --support-root "C:\\Astrid é Data\\runtime"',
    }, ensure_ascii=False).encode("utf-8")
    monkeypatch.setattr(host_bootstrap.subprocess, "run", lambda argv, **kwargs: (
        calls.append((argv, kwargs)) or SimpleNamespace(
            returncode=0,
            stdout=host_bootstrap.base64.b64encode(payload),
        )
    ))
    monkeypatch.setattr(host_bootstrap, "_split_windows_command_line", lambda value: ["parsed", value])

    argv = host_bootstrap._windows_process_argv(4321)

    assert argv[0] == "parsed"
    assert "Astrid é Data" in argv[1]
    assert "Get-CimInstance -ClassName Win32_Process -Filter 'ProcessId = 4321'" in calls[0][0][-1]
    assert "[Console]::OutputEncoding=$utf8" in calls[0][0][-1]
    assert "[Convert]::ToBase64String" in calls[0][0][-1]
    assert calls[0][1]["capture_output"] is True
    assert "encoding" not in calls[0][1]
    assert calls[0][1]["timeout"] == 5.0
    assert calls[0][1]["check"] is False


def test_windows_command_observation_fails_closed_on_output_decode_error(monkeypatch):
    def invalid_output(*_args, **_kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")
    monkeypatch.setattr(host_bootstrap.subprocess, "run", invalid_output)

    with pytest.raises(OSError, match="process command observation failed"):
        host_bootstrap._windows_process_argv(4321)


@pytest.mark.windows_native
def test_windows_native_command_line_parser_preserves_spaces_and_quotes():
    command = 'python.exe -m astrid.core.execution.generic_host run --support-root "C:\\Astrid Data\\runtime" --note "hello world"'
    assert host_bootstrap._split_windows_command_line(command) == [
        "python.exe", "-m", "astrid.core.execution.generic_host", "run",
        "--support-root", "C:\\Astrid Data\\runtime", "--note", "hello world",
    ]


@pytest.mark.windows_native
def test_windows_current_venv_image_matches_selected_pack_host_interpreter():
    observed = host_bootstrap.platform.process_executable(os.getpid())
    selected = host_bootstrap._pack_host_python_executable()
    assert host_bootstrap._same_windows_path(observed, selected)


@pytest.mark.windows_native
def test_windows_venv_python_image_is_captured_separately_from_venv_launcher(tmp_path):
    venv = tmp_path / "venv with spaces"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)],
                   check=True, capture_output=True, text=True, timeout=60)
    selected = venv / "Scripts" / "python.exe"
    proc = subprocess.Popen(
        [str(selected), "-c",
         "import json,sys,time; print(json.dumps({'executable':sys.executable,'base':sys._base_executable}),flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        row = json.loads(proc.stdout.readline())
        image = host_bootstrap.platform.process_executable(proc.pid)
        assert host_bootstrap._same_windows_path(row["executable"], str(selected))
        assert (host_bootstrap._same_windows_path(image, row["executable"])
                or host_bootstrap._same_windows_path(image, row["base"]))
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.mark.windows_native
def test_windows_cim_command_observation_returns_current_python_argv():
    argv = host_bootstrap._windows_process_argv(os.getpid())
    observed = host_bootstrap.platform.process_executable(os.getpid())
    assert argv
    assert host_bootstrap._same_windows_path(argv[0], observed)


@pytest.mark.windows_native
def test_windows_registered_host_binds_real_venv_command_and_support_path(tmp_path):
    venv = tmp_path / "venv with spaces"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)],
                   check=True, capture_output=True, text=True, timeout=60)
    python = venv / "Scripts" / "python.exe"
    package = tmp_path / "stub package"
    module = package / "astrid" / "core" / "execution"
    module.mkdir(parents=True)
    for init in (package / "astrid" / "__init__.py", package / "astrid" / "core" / "__init__.py",
                 module / "__init__.py"):
        init.write_text("", encoding="utf-8")
    (module / "generic_host.py").write_text(
        "import json,os,sys,time\n"
        "from pathlib import Path\n"
        "path=Path(sys.argv[sys.argv.index('--metadata-file')+1])\n"
        "path.write_text(json.dumps({'pid':os.getpid(),'executable':sys.executable,'base':sys._base_executable}))\n"
        "time.sleep(60)\n", encoding="utf-8")
    support = tmp_path / "Astrid é data" / "runtime"
    support.mkdir(parents=True)
    metadata = tmp_path / "host metadata.json"
    child_output = tmp_path / "host stdout stderr.log"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(package)
    # `python -m` prepends its current directory to sys.path. Run from the
    # temporary stub package so it wins over the checkout's real Astrid
    # package; otherwise this fixture imports the real host and fails on its
    # unrelated service dependencies before it can publish test metadata.
    with child_output.open("wb") as output_file:
        process = subprocess.Popen(
            [str(python), "-X", "utf8", "-m", host_bootstrap._HOST_MODULE, "run",
             "--support-root", str(support), "--metadata-file", str(metadata)],
            cwd=package, env=env, stdin=subprocess.DEVNULL,
            stdout=output_file, stderr=subprocess.STDOUT,
        )
    host_pid = None
    host_identity = None
    try:
        deadline = time.monotonic() + 10
        while not metadata.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.02)
        if not metadata.exists():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            output_tail = child_output.read_bytes()[-4096:].decode("utf-8", errors="replace")
            pytest.fail(
                "temporary Windows venv host did not publish its PID; "
                f"returncode={process.returncode}, output_tail={output_tail!r}"
            )
        row = json.loads(metadata.read_text(encoding="utf-8"))
        host_pid = row["pid"]
        host_identity = host_bootstrap.platform.process_identity(host_pid)
        assert host_identity is not None
        state = {
            "pid": host_pid,
            "process_birth_id": host_identity["birth_id"],
            "process_image": host_bootstrap.platform.process_executable(host_pid),
            "python_executable": str(python),
            "support_root": str(support),
        }
        observed_argv = host_bootstrap._windows_process_argv(host_pid)
        status = host_bootstrap._registered_host_status(state)
        assert status == "ours", (
            f"registered host classified {status!r}; argv={observed_argv!r}; "
            f"expected_executable={str(python)!r}; process_image={state['process_image']!r}; "
            f"support_root={str(support)!r}"
        )
        assert host_bootstrap._registered_host_status({**state, "support_root": str(tmp_path / "other")}) == "foreign"
    finally:
        if host_pid is not None and host_identity is not None:
            try:
                host_bootstrap.platform.signal_process(host_identity, host_bootstrap.platform.KILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


def test_process_census_catches_declared_failure_without_hiding_programming_errors(monkeypatch):
    def unavailable():
        raise host_bootstrap.CleanupUncertainError("census unavailable")

    monkeypatch.setattr(host_bootstrap, "_process_snapshot", unavailable)
    assert host_bootstrap._host_birth_identity(4321) == ""
    assert host_bootstrap._descendant_snapshot(4321) == {}

    def broken():
        raise RuntimeError("invalid census implementation")

    # Main also tolerates OSError and ValueError from a census; a bug such as this one still surfaces.
    monkeypatch.setattr(host_bootstrap, "_process_snapshot", broken)
    with pytest.raises(RuntimeError, match="invalid census implementation"):
        host_bootstrap._host_birth_identity(4321)
    with pytest.raises(RuntimeError, match="invalid census implementation"):
        host_bootstrap._descendant_snapshot(4321)
