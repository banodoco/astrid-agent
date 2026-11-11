"""A pack host is acted on by its registration, whichever process launched it."""
from __future__ import annotations

import json
import io
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.product_resources import product_resource
from astrid.core._shared.boot_manifest import load_boot_manifest_hash
from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import (
    source_checkout_closure_digest,
    source_checkout_digest,
)
from astrid.sdk import host_bootstrap
from tests.sdk.test_host_bootstrap_source_identity import (
    _materialize_source_closure as _materialize_host_closure,
)

FAKE_PID = 4242
MODULE_ARGS = ["-m", "astrid.core.execution.generic_host", "run", "--support-root"]
# A leader that optionally keeps one child running, then idles.
LEADER = ("import subprocess,sys,time;"
          "c=subprocess.Popen([sys.executable,'-c','import sys;sys.stdin.buffer.read()'],stdin=sys.stdin,start_new_session=sys.argv[1]=='task') if sys.argv[1] in ('child','task') else None;"
          "open(sys.argv[2],'w').write(str(c.pid if c else 0));"
          "time.sleep(300 if sys.argv[3]=='stay' else 0)")


def _materialize_source_closure(root: Path) -> None:
    _materialize_host_closure(root)
    fixture_matrix = root / "astrid" / "resources" / "astrid-beta-capabilities.json"
    fixture_matrix.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(product_resource("astrid-beta-capabilities.json"), fixture_matrix)


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.tmp, self.mp = tmp_path, monkeypatch
        monkeypatch.delenv("ASTRID_TIMELINE_SCHEMA_PYTHONPATH", raising=False)
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
        # The fake host writes no Remotion launch marker, so ambient Remotion provisioning must not
        # make the reuse path refuse it; the Remotion gate has its own test below.
        monkeypatch.delenv("ASTRID_REMOTION_PROJECT_DIR", raising=False)
        inventory = SimpleNamespace(identity="", roots=(), sources=())
        monkeypatch.setattr("astrid.core.pack.source_setup.active_source_inventory", lambda: inventory)

        class Client:
            def __init__(self, *_a): pass
            def health(self):
                return {"status": "ok", "runtime_epoch": "epoch-1", "runtime_instance_id": "instance-1",
                        "schema_digest": "schema-1", "runtime_session_id": "session-1"}
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
        monkeypatch.setattr(host_bootstrap, "_observe_resident_host", lambda **_kwargs: {
            "executor_id": host_bootstrap.PACK_HOST_ACTOR, "readiness": "ready",
            "runtime_epoch": "epoch-1", "runtime_session_id": "session-1",
            "source_epoch": "source-epoch-1", "source_digest": "source-digest-1",
            "dependency_digest": "dependency-digest-1", "capabilities": [{"status": "ready"}],
        })
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


def test_copied_registration_is_foreign_and_its_markers_are_dropped(tmp_path):
    support = tmp_path / "runtime-r1"
    other = tmp_path / "runtime-r0"
    support.mkdir()
    other.mkdir()
    copied = {"support_root": str(other), "pid": 4242}
    state_path = support / "generic-host.json"
    ready_path = support / "generic-host.ready.json"
    state_path.write_text(json.dumps(copied))
    ready_path.write_text("{}")

    assert host_bootstrap._same_support_root(copied, support) is False
    assert host_bootstrap._same_support_root({"support_root": str(support)}, support) is True

    host_bootstrap._drop_foreign_registration(state_path, ready_path)
    assert not state_path.exists()
    assert not ready_path.exists()
    assert other.is_dir()


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
    markers = [(h.support / name).read_bytes() for name in ("generic-host.json", "generic-host.ready.json")]

    result = host_bootstrap.ensure_pack_host(
        h.value, reconfigure_action="reconfigure", attach_healthy=True,
    )

    assert result["host_status"] == "ready"
    assert result["host_pid"] == FAKE_PID
    assert result["host_source_checkout_digest"] == source_checkout_digest(h.source)
    assert h.launches == []
    assert markers == [(h.support / name).read_bytes() for name in ("generic-host.json", "generic-host.ready.json")]


def test_observer_uses_bound_runtime_arguments(monkeypatch, tmp_path):
    from astrid.sdk import autobootstrap
    from astrid_runtime.paths import RuntimePaths
    assert RuntimePaths.current(data_root=tmp_path).runtime_support == tmp_path / "runtime"
    monkeypatch.setattr(autobootstrap, "_launcher_command", lambda: ["runtime-cli"])
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        payload = json.dumps({"executor_id": "astrid-pack-host", "readiness": "ready",
            "runtime_epoch": 3, "runtime_session_id": "session-1", "source_epoch": "source-1",
            "source_digest": "source", "dependency_digest": "dependency", "capabilities": [{"status": "ready"}]}).encode()
        return _ObserverProcess(payload, 0)
    monkeypatch.setattr(host_bootstrap.subprocess, "Popen", run)
    monkeypatch.setattr(host_bootstrap, "_read_observer_pipe", lambda process, deadline: process.stdout.read())
    assert host_bootstrap._observe_resident_host(
        endpoint="http://runtime.test", support_root=tmp_path / "runtime",
        runtime_instance_id="instance-1", runtime_session_id="session-1", runtime_epoch=3,
    )["executor_id"] == "astrid-pack-host"
    assert calls[0][0] == ["runtime-cli", "observe-pack-host", "--data-root", str(tmp_path),
                           "--endpoint", "http://runtime.test", "--runtime-instance-id", "instance-1",
                           "--runtime-session-id", "session-1", "--runtime-epoch", "3", "--json"]
    assert calls[0][1]["stdin"] == subprocess.DEVNULL


class _ObserverProcess:
    def __init__(self, output, returncode):
        self.stdout, self.returncode = io.BytesIO(output), returncode
    def __enter__(self): return self
    def __exit__(self, *_args): pass
    def wait(self, timeout=None): return self.returncode


@pytest.mark.parametrize("output,returncode", [
    (b"{}", 0), (b"not json", 0), (b"x" * 65537, 0), (b"secret", 1),
])
def test_observer_rejects_bad_output_without_exposing_it(monkeypatch, tmp_path, output, returncode):
    from astrid.sdk import autobootstrap
    monkeypatch.setattr(autobootstrap, "_launcher_command", lambda: ["runtime-cli"])
    def run(_argv, **kwargs):
        return _ObserverProcess(output, returncode)
    monkeypatch.setattr(host_bootstrap.subprocess, "Popen", run)
    def read(process, deadline):
        data = process.stdout.read()
        if process.returncode != 0 or len(data) > 65536:
            raise ValueError("observer failed")
        return data
    monkeypatch.setattr(host_bootstrap, "_read_observer_pipe", read)
    with pytest.raises(RuntimeError, match="runtime observer unavailable") as exc:
        host_bootstrap._observe_resident_host(endpoint="http://runtime.test", support_root=tmp_path / "runtime",
            runtime_instance_id="instance-1", runtime_session_id="session-1", runtime_epoch=3)
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("code", [
    "import sys,time;sys.stdout.write('partial');sys.stdout.flush();time.sleep(10)",
    "import sys;sys.stdout.write('x'*1000000);sys.stdout.flush()",
])
def test_observer_limits_a_real_child(monkeypatch, tmp_path, code):
    from astrid.sdk import autobootstrap
    monkeypatch.setattr(autobootstrap, "_launcher_command", lambda: [sys.executable, "-c", code])
    monkeypatch.setattr(host_bootstrap, "_OBSERVER_TIMEOUT_SECONDS", 0.5)
    with pytest.raises(RuntimeError, match="runtime observer unavailable"):
        host_bootstrap._observe_resident_host(endpoint="http://runtime.test", support_root=tmp_path / "runtime",
            runtime_instance_id="instance-1", runtime_session_id="session-1", runtime_epoch=3)
    assert not any(t.name == "astrid-observer-reader" for t in threading.enumerate())


def test_attach_does_not_require_worker_to_observe_executor(harness, monkeypatch, tmp_path):
    """The worker can handshake but the Runtime reserves executor reads for owner."""
    from astrid_runtime_client.generated import ApiError

    h = harness
    second = tmp_path / "second-source"
    (second / "astrid" / "packs").mkdir(parents=True)
    _materialize_source_closure(second)
    h.value["source_checkout"] = str(second)
    client_type = generic_host.RuntimeProtocolClient

    class ScopedClient(client_type):
        def __init__(self, endpoint, credential):
            super().__init__(endpoint, credential)
            self.credential = credential

        @property
        def generated(self):
            def denied(_executor_id):
                raise ApiError(403, "forbidden", "owner required")
            return SimpleNamespace(get_executor=denied)

    monkeypatch.setattr(generic_host, "RuntimeProtocolClient", ScopedClient)
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "ours")
    monkeypatch.setattr(host_bootstrap, "_retire_host_locked",
                        lambda *_args, **_kwargs: pytest.fail("healthy resident must not be retired"))

    result = host_bootstrap.ensure_pack_host(h.value, reconfigure_action="reconfigure", attach_healthy=True)

    assert result["host_status"] == "ready"
    assert result["host_pid"] == FAKE_PID
    assert h.launches == []


@pytest.mark.parametrize("fault", [
    "unavailable", "foreign", "wrong-epoch", "wrong-session", "unready", "wrong-source",
    "wrong-source-digest", "wrong-dependency", "no-ready-capability",
])
def test_attach_refusal_preserves_resident(harness, monkeypatch, tmp_path, fault):
    h = harness
    second = tmp_path / "second-source"
    (second / "astrid" / "packs").mkdir(parents=True)
    _materialize_source_closure(second)
    h.value["source_checkout"] = str(second)
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "ours")
    before = [(h.support / name).read_bytes() for name in ("generic-host.json", "generic-host.ready.json")]
    monkeypatch.setattr(host_bootstrap, "_retire_host_locked",
                        lambda *_args, **_kwargs: pytest.fail("resident must not be retired"))
    def observe(**_kwargs):
        if fault == "unavailable":
            raise RuntimeError("unavailable")
        value = {"executor_id": host_bootstrap.PACK_HOST_ACTOR, "readiness": "ready",
                 "runtime_epoch": "epoch-1", "runtime_session_id": "session-1",
                 "source_epoch": "source-epoch-1", "source_digest": "source-digest-1",
                 "dependency_digest": "dependency-digest-1", "capabilities": [{"status": "ready"}]}
        field, replacement = {"foreign": ("executor_id", "foreign"),
                              "wrong-epoch": ("runtime_epoch", "epoch-2"),
                              "wrong-session": ("runtime_session_id", "session-2"),
                              "unready": ("readiness", "starting"),
                              "wrong-source": ("source_epoch", "source-epoch-2"),
                              "wrong-source-digest": ("source_digest", "other"),
                              "wrong-dependency": ("dependency_digest", "other"),
                              "no-ready-capability": ("capabilities", [])}[fault]
        value[field] = replacement
        return value
    monkeypatch.setattr(host_bootstrap, "_observe_resident_host", observe)
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="attachment was refused"):
        host_bootstrap.ensure_pack_host(h.value, reconfigure_action="reconfigure", attach_healthy=True)
    assert before == [(h.support / name).read_bytes() for name in ("generic-host.json", "generic-host.ready.json")]
    assert h.launches == []


def test_bad_worker_never_invokes_observer(harness, monkeypatch, tmp_path):
    h = harness
    second = tmp_path / "second-source"
    (second / "astrid" / "packs").mkdir(parents=True)
    _materialize_source_closure(second)
    h.value["source_checkout"] = str(second)
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "ours")
    class InvalidWorker(generic_host.RuntimeProtocolClient):
        def _authenticate_worker(self, _executor_id):
            raise RuntimeError("invalid worker")
    monkeypatch.setattr(generic_host, "RuntimeProtocolClient", InvalidWorker)
    monkeypatch.setattr(host_bootstrap, "_observe_resident_host",
                        lambda **_kwargs: pytest.fail("observer must follow worker authentication"))
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="attachment was refused"):
        host_bootstrap.ensure_pack_host(h.value, reconfigure_action="reconfigure", attach_healthy=True)
    assert h.launches == []


def test_unmanaged_changed_resident_source_can_be_replaced(harness, monkeypatch, tmp_path):
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
    monkeypatch.setattr(host_bootstrap, "_retire_host_locked",
                        lambda state, support, **_kwargs: retired.append(dict(state)))
    (resident_source / "astrid" / "packs" / "changed.marker").write_text("changed")
    result = host_bootstrap.ensure_pack_host(h.value, reconfigure_action="reconfigure", attach_healthy=True)
    assert result["host_status"] == "ready"
    assert len(retired) == len(h.launches) == 1


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
    monkeypatch.setattr(host_bootstrap, "_retire_host_locked", lambda state, support: retired.append(dict(state)))

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
    monkeypatch.setattr(host_bootstrap, "_retire_host_locked", lambda state, support: retired.append(dict(state)))

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

    (tmp_path / "generic-host.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="no retained Job custody"):
        host_bootstrap._retire_host_locked(state, tmp_path)
    assert all(marker.exists() for marker in markers)


@pytest.mark.parametrize("observed", [None, {"pid": 4321, "birth_id": "windows-creation:9999"}])
def test_windows_prior_runtime_retirement_drops_markers_without_signalling(monkeypatch, tmp_path, observed):
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    monkeypatch.setenv("ASTRID_RUNTIME_SERVICE_NAME", "pack-host")
    monkeypatch.setattr(host_bootstrap.platform, "process_identity", lambda _pid: observed)
    monkeypatch.setattr(host_bootstrap, "terminate_group", lambda *_a, **_kw: pytest.fail("signalled a Job"))
    monkeypatch.setattr(host_bootstrap, "_stop_group", lambda *_a, **_kw: pytest.fail("signalled a PID"))
    monkeypatch.setattr(host_bootstrap.os, "kill", lambda *_a, **_kw: pytest.fail("signalled a PID"))
    state = {"pid": 4321, "process_birth_id": "windows-creation:1234", "support_root": str(tmp_path),
             "runtime_instance_id": "old-runtime", "runtime_service_launch_id": "a" * 32}
    markers = [tmp_path / "generic-host.json", tmp_path / "generic-host.ready.json"]
    for marker in markers:
        marker.write_text("{}", encoding="utf-8")

    (tmp_path / "generic-host.json").write_text(json.dumps(state), encoding="utf-8")
    host_bootstrap._retire_host_locked(state, tmp_path, foreground=True, current_runtime_instance_id="new-runtime")

    assert all(not marker.exists() for marker in markers)


@pytest.mark.parametrize("fault", ["same", "missing_old", "missing_current", "blank_old", "blank_current",
                                  "missing_launch", "bad_launch", "missing_birth", "bad_birth", "missing_pid", "wrong_service"])
def test_windows_prior_runtime_retirement_refuses_ambiguous_identity(monkeypatch, tmp_path, fault):
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    monkeypatch.setenv("ASTRID_RUNTIME_SERVICE_NAME", "pack-host")
    monkeypatch.setattr(host_bootstrap.platform, "process_identity", lambda _pid: None)
    monkeypatch.setattr(host_bootstrap, "terminate_group", lambda *_a, **_kw: pytest.fail("signalled a Job"))
    monkeypatch.setattr(host_bootstrap.os, "kill", lambda *_a, **_kw: pytest.fail("signalled a PID"))
    state = {"pid": 4321, "process_birth_id": "windows-creation:1234", "support_root": str(tmp_path),
             "runtime_instance_id": "old-runtime", "runtime_service_launch_id": "a" * 32}
    current = "new-runtime"
    if fault == "same": current = "old-runtime"
    elif fault == "missing_old": del state["runtime_instance_id"]
    elif fault == "missing_current": current = None
    elif fault == "blank_old": state["runtime_instance_id"] = " "
    elif fault == "blank_current": current = " "
    elif fault == "missing_launch": del state["runtime_service_launch_id"]
    elif fault == "bad_launch": state["runtime_service_launch_id"] = "bad"
    elif fault == "missing_birth": del state["process_birth_id"]
    elif fault == "bad_birth": state["process_birth_id"] = "ambiguous"
    elif fault == "missing_pid": del state["pid"]
    elif fault == "wrong_service": monkeypatch.setenv("ASTRID_RUNTIME_SERVICE_NAME", "other")
    markers = [tmp_path / "generic-host.json", tmp_path / "generic-host.ready.json"]
    for marker in markers:
        marker.write_text("{}", encoding="utf-8")

    (tmp_path / "generic-host.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(host_bootstrap.PackHostBootstrapError):
        host_bootstrap._retire_host_locked(state, tmp_path, foreground=True, current_runtime_instance_id=current)
    assert all(marker.exists() for marker in markers)


@pytest.mark.parametrize("status", ["foreign", "ours", "unknown"])
def test_windows_prior_runtime_retirement_refuses_non_stale_status(monkeypatch, tmp_path, status):
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    monkeypatch.setenv("ASTRID_RUNTIME_SERVICE_NAME", "pack-host")
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: status)
    monkeypatch.setattr(host_bootstrap, "terminate_group", lambda *_a, **_kw: pytest.fail("signalled a Job"))
    monkeypatch.setattr(host_bootstrap.os, "kill", lambda *_a, **_kw: pytest.fail("signalled a PID"))
    state = {"pid": 4321, "process_birth_id": "windows-creation:1234", "support_root": str(tmp_path),
             "runtime_instance_id": "old-runtime", "runtime_service_launch_id": "a" * 32}
    marker = tmp_path / "generic-host.json"
    marker.write_text("{}", encoding="utf-8")
    (tmp_path / "generic-host.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(host_bootstrap.PackHostBootstrapError):
        host_bootstrap._retire_host_locked(state, tmp_path, foreground=True, current_runtime_instance_id="new-runtime")
    assert marker.exists()


def test_windows_prior_runtime_retirement_refuses_ambiguous_native_birth(monkeypatch, tmp_path):
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    monkeypatch.setenv("ASTRID_RUNTIME_SERVICE_NAME", "pack-host")
    monkeypatch.setattr(host_bootstrap.platform, "process_identity", lambda _pid: {"pid": 4321})
    state = {"pid": 4321, "process_birth_id": "windows-creation:1234", "support_root": str(tmp_path),
             "runtime_instance_id": "old-runtime", "runtime_service_launch_id": "a" * 32}
    marker = tmp_path / "generic-host.json"
    marker.write_text("{}", encoding="utf-8")
    (tmp_path / "generic-host.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="identity is ambiguous"):
        host_bootstrap._retire_host_locked(state, tmp_path, foreground=True, current_runtime_instance_id="new-runtime")
    assert marker.exists()


@pytest.mark.parametrize("discovery,health,expected", [
    ("instance-1", "instance-1", "instance-1"),
    ("instance-1", "other-instance", None),
    (None, "instance-1", None),
    ("instance-1", None, None),
])
def test_foreground_retirement_uses_only_matching_explicit_runtime_identity(
    harness, monkeypatch, discovery, health, expected,
):
    harness.value["runtime_instance_id"] = discovery
    original_client = generic_host.RuntimeProtocolClient

    class Client(original_client):
        def health(self):
            return {**super().health(), "runtime_instance_id": health}

    monkeypatch.setattr(generic_host, "RuntimeProtocolClient", Client)
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "gone")
    seen = []

    def retire(_state, _support, **kwargs):
        seen.append(kwargs)
        raise host_bootstrap.PackHostBootstrapError("stop at retirement")

    monkeypatch.setattr(host_bootstrap, "_retire_host_locked", retire)
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="stop at retirement"):
        host_bootstrap.ensure_pack_host(harness.value, reconfigure_action="reconfigure", _foreground=True)
    assert seen == [{"foreground": True, "current_runtime_instance_id": expected}]


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
    registration.write_text(json.dumps(state), encoding="utf-8")

    host_bootstrap._retire_host_locked(state, tmp_path, foreground=True)

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


@pytest.mark.parametrize("mismatched", [False, True])
def test_foreground_retirement_is_bound_to_locked_support(harness, monkeypatch, tmp_path, mismatched):
    other = tmp_path / "other-support"
    other.mkdir()
    names = ("generic-host.json", "generic-host.ready.json")
    other_bytes = b'{"pid":9999,"runtime_instance_id":"unrelated-owner"}'
    for name in names:
        (other / name).write_bytes(other_bytes)
    state = {
        "pid": 4321, "process_birth_id": "windows-creation:1234",
        "support_root": str(other if mismatched else harness.support),
        "runtime_instance_id": "old-runtime", "runtime_service_launch_id": "a" * 32,
    }
    for name in names:
        (harness.support / name).write_text(json.dumps(state), encoding="utf-8")
    before = {name: (harness.support / name).read_bytes() for name in names}
    monkeypatch.setattr(host_bootstrap, "sys", SimpleNamespace(**{**vars(sys), "platform": "win32"}))
    monkeypatch.setenv("ASTRID_RUNTIME_SERVICE_NAME", "pack-host")
    monkeypatch.setattr(host_bootstrap, "_RETAINED_HOSTS", {})
    monkeypatch.setattr(host_bootstrap.platform, "process_identity", lambda _pid: None)
    monkeypatch.setattr(host_bootstrap, "terminate_group", lambda *_a, **_kw: pytest.fail("signalled a Job"))
    monkeypatch.setattr(host_bootstrap.os, "kill", lambda *_a, **_kw: pytest.fail("signalled a PID"))

    class AfterRetirement(Exception):
        pass

    def stop_after_retirement(*_args):
        raise AfterRetirement

    monkeypatch.setattr(host_bootstrap, "_capability_matrix_path", stop_after_retirement)
    if mismatched:
        with pytest.raises(host_bootstrap.PackHostBootstrapError, match="inconsistent host support"):
            host_bootstrap.ensure_pack_host(harness.value, reconfigure_action="reconfigure", _foreground=True)
        assert {name: (harness.support / name).read_bytes() for name in names} == before
    else:
        with pytest.raises(AfterRetirement):
            host_bootstrap.ensure_pack_host(harness.value, reconfigure_action="reconfigure", _foreground=True)
        assert all(not (harness.support / name).exists() for name in names)
    assert all((other / name).read_bytes() == other_bytes for name in names)


def test_stop_registered_host_refuses_superseded_registration(monkeypatch, tmp_path):
    state = {"pid": 4321, "support_root": str(tmp_path)}
    successor = {"pid": 9876, "support_root": str(tmp_path)}
    registration = tmp_path / "generic-host.json"
    readiness = tmp_path / "generic-host.ready.json"
    registration.write_text(json.dumps(successor), encoding="utf-8")
    readiness.write_bytes(b"successor readiness")
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: pytest.fail("inspected stale owner"))

    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="inconsistent host support"):
        host_bootstrap._stop_registered_host(state)

    assert json.loads(registration.read_text(encoding="utf-8")) == successor
    assert readiness.read_bytes() == b"successor readiness"


def test_runtime_shutdown_retires_the_registered_host_synchronously(harness):
    from astrid.services import prepare_pack_host_shutdown
    h = harness
    process, _ = h.host()
    h.register(process.pid)
    state = h.marker()
    stop = prepare_pack_host_shutdown(support_root=h.support, source_checkout=h.source,
                                     runtime_instance_id=state["runtime_instance_id"])
    stop()
    assert process.poll() is not None
    assert not (h.support / "generic-host.json").exists()
    assert not (h.support / "generic-host.ready.json").exists()


def test_runtime_shutdown_preserves_replacement_registration(harness):
    from astrid.services import prepare_pack_host_shutdown
    h = harness
    process, _ = h.host()
    h.register(process.pid)
    state = h.marker()
    stop = prepare_pack_host_shutdown(support_root=h.support, source_checkout=h.source,
                                     runtime_instance_id=state["runtime_instance_id"])
    replacement = {**state, "runtime_instance_id": "replacement-owner"}
    (h.support / "generic-host.json").write_text(json.dumps(replacement))
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="registration changed"):
        stop()
    assert process.poll() is None and h.marker() == replacement


@pytest.mark.parametrize("field,value", [("runtime_instance_id", "foreign-owner"), ("source_checkout", "foreign-source")])
def test_runtime_shutdown_refuses_foreign_owner_before_effects(harness, field, value):
    from astrid.services import prepare_pack_host_shutdown
    h = harness
    process, _ = h.host()
    h.register(process.pid, **{field: value})
    state = h.marker()
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="selected Runtime owner"):
        prepare_pack_host_shutdown(support_root=h.support, source_checkout=h.source,
                                   runtime_instance_id="instance-1")
    assert process.poll() is None and h.marker() == state


def test_runtime_shutdown_preserves_unscoped_absence(harness):
    from astrid.services import prepare_pack_host_shutdown
    h = harness
    process, _ = h.host()
    h.kill_leader(process)
    state = h.marker()
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="process scope is unverified"):
        prepare_pack_host_shutdown(support_root=h.support, source_checkout=h.source,
                                   runtime_instance_id=state["runtime_instance_id"])
    assert h.marker() == state


def _supervise(h) -> None:
    """Mark the registration as this Runtime's supervised foreground host."""
    data = h.marker()
    data["runtime_service_launch_id"] = "a" * 32
    (h.support / "generic-host.json").write_text(json.dumps(data), encoding="utf-8")


def _forbid_retirement(monkeypatch) -> None:
    monkeypatch.setattr(host_bootstrap, "_retire_host_locked",
                        lambda *_a, **_kw: pytest.fail("retired a Runtime-supervised host from an SDK caller"))
    monkeypatch.setattr(host_bootstrap, "_terminate_old_host",
                        lambda *_a, **_kw: pytest.fail("signalled a Runtime-supervised host from an SDK caller"))


def test_supervised_starting_host_attaches_once_its_ready_record_appears(harness, monkeypatch, tmp_path):
    h = harness
    second = tmp_path / "second-source"
    (second / "astrid" / "packs").mkdir(parents=True)
    _materialize_source_closure(second)
    h.value["source_checkout"] = str(second)
    _supervise(h)
    ready_record = (h.support / "generic-host.ready.json").read_bytes()
    (h.support / "generic-host.ready.json").unlink()  # still starting: no ready record yet
    polls = []

    def sleep_until_ready(seconds):
        polls.append(seconds)
        if len(polls) == 2:
            (h.support / "generic-host.ready.json").write_bytes(ready_record)

    monkeypatch.setattr(host_bootstrap.time, "sleep", sleep_until_ready)
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "ours")
    monkeypatch.setattr(host_bootstrap, "_host_idle", lambda *_args: True)
    _forbid_retirement(monkeypatch)
    from astrid.core.gateway import dispatch
    monkeypatch.setattr(
        dispatch, "compose_profile_handoff",
        lambda *_args, **_kwargs: pytest.fail("attach must preserve the resident boot manifest"),
    )

    result = host_bootstrap.ensure_pack_host(h.value, reconfigure_action="reconfigure", attach_healthy=True)

    assert result["host_status"] == "ready"
    assert result["host_pid"] == FAKE_PID
    assert len(polls) == 2
    assert h.launches == []


def test_supervised_host_that_never_becomes_ready_is_left_alone(harness, monkeypatch):
    h = harness
    _supervise(h)
    (h.support / "generic-host.ready.json").unlink()
    monkeypatch.setattr(host_bootstrap, "_STARTUP_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(host_bootstrap, "_STARTUP_POLL_SECONDS", 0.01)
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "ours")
    monkeypatch.setattr(host_bootstrap, "_host_idle", lambda *_args: True)
    _forbid_retirement(monkeypatch)

    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="still starting") as raised:
        host_bootstrap.ensure_pack_host(h.value, reconfigure_action="reconfigure", attach_healthy=True)

    assert "no retained Job custody" not in str(raised.value)
    assert str(FAKE_PID) in str(raised.value)
    assert h.launches == []


def test_supervised_host_that_dies_while_starting_raises_clearly(harness, monkeypatch):
    h = harness
    _supervise(h)
    (h.support / "generic-host.ready.json").unlink()
    statuses = iter(["ours", "gone"])
    monkeypatch.setattr(host_bootstrap, "_STARTUP_POLL_SECONDS", 0.01)
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: next(statuses))
    monkeypatch.setattr(host_bootstrap, "_host_idle", lambda *_args: True)
    _forbid_retirement(monkeypatch)

    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="stopped during startup") as raised:
        host_bootstrap.ensure_pack_host(h.value, reconfigure_action="reconfigure", attach_healthy=True)

    assert "no retained Job custody" not in str(raised.value)
    assert h.launches == []


def test_stale_supervised_registration_is_left_to_the_normal_path(harness, monkeypatch):
    h = harness
    _supervise(h)
    (h.support / "generic-host.ready.json").unlink()
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "gone")
    monkeypatch.setattr(host_bootstrap.time, "sleep", lambda _s: pytest.fail("a stale host must not be waited on"))

    host_bootstrap._await_supervised_startup(h.support / "generic-host.json",
                                             h.support / "generic-host.ready.json", lambda: {})


STARTING_ROW = {"name": "pack-host", "state": "starting", "last_exit": None, "restart_count": 0, "reason": None}
RUNNING_ROW = {"name": "pack-host", "state": "running", "last_exit": None, "restart_count": 0, "reason": None}


def _runtime_supervises(monkeypatch, rows, *, typed: bool = False) -> None:
    """Make the Runtime's health report these service rows, as a dict or as the typed Health."""
    base = {"status": "ok", "protocol": "workspace.v1", "schema_digest": "schema-1",
            "runtime_epoch": "epoch-1", "runtime_session_id": "session-1",
            "runtime_instance_id": "instance-1", "identity": None}

    def health(self):
        if typed:
            return SimpleNamespace(**base, services=tuple(SimpleNamespace(**row) for row in rows))
        return {**base, "services": [dict(row) for row in rows]}

    monkeypatch.setattr(generic_host.RuntimeProtocolClient, "health", health)


def test_runtime_supervised_host_registered_late_attaches_once_ready(harness, monkeypatch, tmp_path):
    h = harness
    second = tmp_path / "second-source"
    (second / "astrid" / "packs").mkdir(parents=True)
    _materialize_source_closure(second)
    h.value["source_checkout"] = str(second)
    registration = (h.support / "generic-host.json").read_bytes()
    ready_record = (h.support / "generic-host.ready.json").read_bytes()
    (h.support / "generic-host.json").unlink()  # launch validation still running: no registration yet
    (h.support / "generic-host.ready.json").unlink()
    _runtime_supervises(monkeypatch, [STARTING_ROW], typed=True)
    polls = []

    def host_progress(seconds):
        polls.append(seconds)
        if len(polls) == 1:
            (h.support / "generic-host.json").write_bytes(registration)  # registered, no ready record
        if len(polls) == 3:
            (h.support / "generic-host.ready.json").write_bytes(ready_record)

    monkeypatch.setattr(host_bootstrap.time, "sleep", host_progress)
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "ours")
    monkeypatch.setattr(host_bootstrap, "_host_idle", lambda *_args: True)
    _forbid_retirement(monkeypatch)
    from astrid.core.gateway import dispatch
    monkeypatch.setattr(
        dispatch, "compose_profile_handoff",
        lambda *_args, **_kwargs: pytest.fail("attach must preserve the resident boot manifest"),
    )

    result = host_bootstrap.ensure_pack_host(h.value, reconfigure_action="reconfigure", attach_healthy=True)

    assert result["host_status"] == "ready"
    assert result["host_pid"] == FAKE_PID
    assert len(polls) == 3
    assert h.launches == []


def test_runtime_supervised_host_that_never_becomes_ready_is_left_alone(harness, monkeypatch):
    h = harness
    (h.support / "generic-host.ready.json").unlink()
    _runtime_supervises(monkeypatch, [RUNNING_ROW])  # the Runtime owns the host; its registration has no launch id
    monkeypatch.setattr(host_bootstrap, "_STARTUP_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(host_bootstrap, "_STARTUP_POLL_SECONDS", 0.01)
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "ours")
    monkeypatch.setattr(host_bootstrap, "_host_idle", lambda *_args: True)
    _forbid_retirement(monkeypatch)

    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="still starting") as raised:
        host_bootstrap.ensure_pack_host(h.value, reconfigure_action="reconfigure", attach_healthy=True)

    assert "no retained Job custody" not in str(raised.value)
    assert str(FAKE_PID) in str(raised.value)
    assert h.launches == []


def test_runtime_supervised_host_whose_service_failed_names_the_reason(harness, monkeypatch):
    h = harness
    (h.support / "generic-host.json").unlink()
    (h.support / "generic-host.ready.json").unlink()
    _runtime_supervises(monkeypatch, [{"name": "pack-host", "state": "failed", "last_exit": 1,
                                       "restart_count": 3, "reason": "pack host exited with status 1"}])
    _forbid_retirement(monkeypatch)

    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="failed") as raised:
        host_bootstrap.ensure_pack_host(h.value, reconfigure_action="reconfigure", attach_healthy=True)

    assert "pack host exited with status 1" in str(raised.value)
    assert h.launches == []


def test_no_supervisor_and_no_registration_keeps_the_launch_path(harness, monkeypatch):
    h = harness
    (h.support / "generic-host.json").unlink()
    (h.support / "generic-host.ready.json").unlink()
    _runtime_supervises(monkeypatch, [])

    result = host_bootstrap.ensure_pack_host(h.value, reconfigure_action="reconfigure", attach_healthy=True)

    assert result["host_status"] == "ready"
    assert len(h.launches) == 1

@pytest.mark.skipif(os.name == "nt", reason="POSIX inherited pipe fixture")
def test_observer_refuses_descendant_open_pipe_at_deadline(monkeypatch, tmp_path):
    from astrid.sdk import autobootstrap
    code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time;time.sleep(3)'])"
    monkeypatch.setattr(autobootstrap, "_launcher_command", lambda: [sys.executable, "-c", code])
    monkeypatch.setattr(host_bootstrap, "_OBSERVER_TIMEOUT_SECONDS", 0.2)
    started = time.monotonic()
    with pytest.raises(host_bootstrap.ObserverCleanupUncertain, match="cleanup uncertain"):
        host_bootstrap._observe_resident_host(endpoint="http://runtime.test", support_root=tmp_path / "runtime",
            runtime_instance_id="instance-1", runtime_session_id="session-1", runtime_epoch=3)
    assert host_bootstrap._UNCERTAIN_OBSERVERS[-1].poll() == 0
    assert time.monotonic() - started < 1.0
