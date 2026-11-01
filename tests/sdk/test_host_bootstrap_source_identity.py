from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.sdk import host_bootstrap
from astrid.core.execution.generic_host import (
    source_checkout_closure_digest,
    source_checkout_digest,
)
from astrid.core._shared.boot_manifest import load_boot_manifest_hash


def _mock_host_launch(monkeypatch, fake_popen) -> None:
    # Fake process fixtures do not have a kernel command. Dedicated cases below
    # exercise publication and later-process recognition using native observations.
    # Keep the fake launch local to bootstrap.  Patching the shared subprocess
    # module would also intercept VibeComfy's real git identity probes.
    monkeypatch.setattr(host_bootstrap, "popen_owned_group", lambda argv, **kwargs: fake_popen(argv, **{**kwargs, "start_new_session": True}))
    monkeypatch.setattr(host_bootstrap, "_capture_native_host_launch_identity", lambda pid: {
        "pid": pid, "uid": os.getuid(), "pgid": pid, "start_sec": 1700000000, "start_usec": 1})



def _materialize_source_closure(source: Path) -> None:
    (source / "astrid" / "core" / "execution").mkdir(parents=True)
    (source / "astrid" / "core" / "execution" / "generic_host.py").write_text(
        "# host fixture\n", encoding="utf-8"
    )
    (source / "astrid" / "sdk").mkdir(parents=True)
    (source / "astrid" / "sdk" / "marker.py").write_text(
        "# sdk fixture\n", encoding="utf-8"
    )
    (source / "astrid" / "omp_agent.py").write_text(
        "# launcher fixture\n", encoding="utf-8"
    )
    (source / "astrid" / "__init__.py").write_text("# package fixture\n", encoding="utf-8")
    (source / "astrid" / "version.py").write_text(
        "__version__ = 'fixture'\n", encoding="utf-8"
    )
    (source / "astrid" / "__main__.py").write_text("# main fixture\n", encoding="utf-8")
    (source / "astrid" / "runtime_cli.py").write_text("# runtime cli fixture\n", encoding="utf-8")
    (source / "astrid" / "sdk" / "workspace_client.py").write_text(
        "# workspace client fixture\n", encoding="utf-8"
    )
    (source / "astrid_runtime_client").mkdir()
    (source / "astrid_runtime_client" / "__init__.py").write_text(
        "# vendored client fixture\n", encoding="utf-8"
    )
    (source / "astrid_runtime_client" / "generated.py").write_text(
        "# generated client fixture\n", encoding="utf-8"
    )
    (source / "astrid_runtime_client" / "contract_metadata.py").write_text(
        "# contract fixture\n", encoding="utf-8"
    )


def test_windows_parked_preparation_refuses_before_projection_or_socket_allocation(monkeypatch):
    projected = []
    preparer = host_bootstrap._ParkedPackHostPreparer(lambda reference: projected.append(reference))
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(host_bootstrap.socket, "socketpair", lambda: pytest.fail("socket allocated"))

    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="unavailable on Windows"):
        preparer.prepare(object())

    assert projected == []


@pytest.mark.parametrize("outcome,alive", [("LIVE", True), ("ZOMBIE", False), ("ABSENT", False)])
def test_host_pid_alive_uses_native_darwin_state_without_subprocess(monkeypatch, outcome, alive):
    from astrid.core.execution import custody_broker
    monkeypatch.setattr(host_bootstrap.sys, "platform", "darwin")
    monkeypatch.setattr(custody_broker, "ordinary_candidate_observation", lambda pid: {"outcome": outcome})
    def forbidden(*args, **kwargs):
        raise AssertionError("Darwin host state must not launch ps or signal")
    monkeypatch.setattr(host_bootstrap.os, "popen", forbidden)
    monkeypatch.setattr(host_bootstrap.os, "kill", forbidden)
    assert host_bootstrap._host_pid_alive(4242) is alive


def test_host_pid_alive_native_failure_does_not_become_absence(monkeypatch):
    from astrid.core.execution import custody_broker
    monkeypatch.setattr(host_bootstrap.sys, "platform", "darwin")
    monkeypatch.setattr(custody_broker, "ordinary_candidate_observation", lambda pid: {"outcome": "OBSERVATION_ERROR"})
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="native state observation unavailable"):
        host_bootstrap._host_pid_alive(4242)


@pytest.mark.parametrize("acl_allows", [True, False])
def test_parked_grant_uses_owner_only_acl_on_windows_modes(monkeypatch, tmp_path, acl_allows):
    from astrid.core.execution import generic_host

    credential = tmp_path / "credential"
    credential.write_text("token")
    credential.chmod(0o600)
    real_stat = Path.stat
    def windows_stat(candidate, *args, **kwargs):
        result = real_stat(candidate, *args, **kwargs)
        if candidate == credential:
            return os.stat_result((__import__("stat").S_IFREG | 0o444, *result[1:]))
        return result
    monkeypatch.setattr(Path, "stat", windows_stat)
    monkeypatch.setattr(host_bootstrap.platform, "is_owner_only", lambda _path: acl_allows)
    monkeypatch.setattr(host_bootstrap, "_parked_host_identity_matches", lambda _state: True)
    grant = {"activation_id": "activation", "credential_file": str(credential),
             "executor_incarnation": "incarnation", "evidence_digest": "digest"}
    process = SimpleNamespace(pid=4321, poll=lambda: None)
    control = SimpleNamespace(settimeout=lambda _value: None, recv=lambda _size: b"",
                              close=lambda: None, shutdown=lambda _how: None)
    handle = host_bootstrap._ParkedPackHostHandle(
        process, control, None,
        {"credential_file": str(credential), "process_birth_id": "birth"}, "op", "channel",
    )
    preparer = host_bootstrap._ParkedPackHostPreparer(lambda *_args: None)
    preparer._handle = handle
    sent = []
    monkeypatch.setattr(generic_host, "_send_activation_frame", lambda _control, frame: sent.append(frame))
    monkeypatch.setattr(generic_host, "_read_activation_frame", lambda _control: {
        "version": generic_host._ACTIVATION_ACCEPTED_VERSION, "operation_id": "op",
        "channel_id": "channel", "activation_id": "activation",
        "executor_incarnation": "incarnation", "evidence_digest": "digest",
        "host": {"pid": 4321, "birth_id": "birth"},
    })
    if acl_allows:
        assert preparer.acknowledge(handle, grant)["activation_id"] == "activation"
        assert sent
    else:
        with pytest.raises(host_bootstrap.PackHostBootstrapError, match="credential reference is unsafe"):
            preparer.acknowledge(handle, grant)
        assert not sent


def test_installed_capability_matrix_uses_only_a_regular_pinned_resource(tmp_path, monkeypatch):
    source = tmp_path / "py" / "Lib" / "site-packages"
    source.mkdir(parents=True)
    provenance = tmp_path / "a"
    config = provenance / "config"
    config.mkdir(parents=True)
    matrix = config / "astrid-beta-capabilities.json"
    matrix.write_text('{"schema_version":1,"capabilities":[]}\n', encoding="utf-8")
    monkeypatch.setenv("ASTRID_CAPABILITY_MATRIX_PATH", str(matrix))
    monkeypatch.setenv("ASTRID_SOURCE_PROVENANCE_ROOT", str(provenance))

    assert host_bootstrap._capability_matrix_path(source) == matrix.resolve()
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("ASTRID_CAPABILITY_MATRIX_PATH", str(elsewhere))
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="outside its source provenance root"):
        host_bootstrap._capability_matrix_path(source)


def test_capability_matrix_default_stays_relative_to_source_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRID_CAPABILITY_MATRIX_PATH", raising=False)
    monkeypatch.delenv("ASTRID_SOURCE_PROVENANCE_ROOT", raising=False)
    source = tmp_path / "checkout"
    assert host_bootstrap._capability_matrix_path(source) == source / "config" / "astrid-beta-capabilities.json"


def test_selected_python_matches_its_live_process_executable(tmp_path) -> None:
    """Framework Python re-execs Python.app; the selected launcher stays valid."""
    import shlex
    import sys

    selected = Path(sys.executable).resolve()
    process = subprocess.Popen(
        [str(selected), "-I", "-S", "-c", "import time, sys; print('ready'); sys.stdout.flush(); time.sleep(30)"],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        from astrid.core.execution.custody_broker import _command_observation
        import json
        (tmp_path / "original-child-native-observation.json").write_text(json.dumps(_command_observation(process.pid), indent=2))
        executable = shlex.split(host_bootstrap._host_command(process.pid))[0]
        assert host_bootstrap._host_executable_matches(executable, selected)
        assert not host_bootstrap._host_executable_matches("/unrelated/Python", selected)
    finally:
        process.terminate()
        process.wait(timeout=5)
        process.stdout.close()


def test_executable_probe_failure_does_not_accept_another_python(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(host_bootstrap.sys, "platform", "darwin")

    def fail_probe(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(host_bootstrap, "subprocess", SimpleNamespace(
        **{**vars(subprocess), "run": fail_probe},
    ))
    assert not host_bootstrap._host_executable_matches("/unrelated/Python", tmp_path / "python")


@pytest.mark.parametrize("windows_acl", [None, True, False])
def test_bootstrap_passes_inventory_identity_and_restarts_on_change(monkeypatch, tmp_path: Path, windows_acl) -> None:
    source = tmp_path / "source"
    (source / "astrid" / "packs").mkdir(parents=True)
    (source / "astrid" / "packs" / "marker.txt").write_text("pack", encoding="utf-8")
    _materialize_source_closure(source)
    managed = tmp_path / "managed-pack"
    managed.mkdir()
    support = tmp_path / "support" / "nested"
    support.mkdir(parents=True)
    credential = support.parent / "worker.token"
    credential.write_text("worker", encoding="utf-8")
    os.chmod(credential, 0o600)
    if windows_acl is not None:
        real_stat = Path.stat
        def windows_stat(candidate, *args, **kwargs):
            result = real_stat(candidate, *args, **kwargs)
            if candidate == credential:
                return os.stat_result((__import__("stat").S_IFREG | 0o666, *result[1:]))
            return result
        monkeypatch.setattr(Path, "stat", windows_stat)
        monkeypatch.setattr(host_bootstrap.platform, "is_owner_only", lambda _path: windows_acl)
    value = {
        "worker_credential_file": str(credential),
        "source_checkout": str(source),
        "worker_actor": host_bootstrap.PACK_HOST_ACTOR,
        "worker_scopes": list(host_bootstrap.PACK_HOST_SCOPES),
        "endpoint": "http://runtime.test",
        "runtime_epoch": "epoch-1",
        "runtime_instance_id": "instance-1",
        "schema_digest": "schema-1",
    }
    inventory = SimpleNamespace(identity="inventory-1", roots=(managed,), sources=(managed,))
    inventory_calls: list[object] = []

    def active_inventory():
        inventory_calls.append(inventory)
        return inventory

    monkeypatch.setattr("astrid.core.pack.source_setup.active_source_inventory", active_inventory)
    from astrid.core.execution import generic_host
    class FakeRuntimeClient:
        def __init__(self, endpoint, credential):
            assert endpoint == "http://runtime.test"
            assert credential == "worker"

        def health(self):
            return {"status": "ok", "runtime_epoch": "epoch-1", "runtime_instance_id": "instance-1", "schema_digest": "schema-1"}

    monkeypatch.setattr(generic_host, "RuntimeProtocolClient", FakeRuntimeClient)
    state: dict = {}
    ready: dict = {}
    launches: list[list[str]] = []
    terminated: list[dict] = []

    class FakeProcess:
        pid = 4242

        def poll(self):
            return None

    def fake_read(path: Path):
        if path.name == "generic-host.json":
            return state or None
        return ready or None

    def fake_write(path: Path, payload: dict):
        state.update(payload)

    def fake_popen(argv, **_kwargs):
        launches.append(list(argv))
        boot_manifest = credential.parent.parent / "astrid-host" / "boot-manifest.json"
        ready.update({
            "status": "ready",
            "pid": 4242,
            "process_birth_id": "birth-1",
            "python_executable": os.path.abspath(__import__("sys").executable),
            "endpoint": "http://runtime.test",
            "executor_id": host_bootstrap.PACK_HOST_ACTOR,
                "ready_file": str(credential.parent.parent / "generic-host.ready.json"),
            "credential_file": str(credential),
                "support_root": str(credential.parent.parent),
            "source_checkout": str(source),
            "source_checkout_digest": source_checkout_digest(source),
            "source_closure_digest": source_checkout_closure_digest(source),
            "source_inventory_identity": inventory.identity,
            "boot_manifest_path": str(boot_manifest),
            "boot_manifest_hash": load_boot_manifest_hash(
                boot_manifest, support_root=credential.parent.parent
            ),
            "runtime_instance_id": "instance-1",
            "runtime_epoch": "epoch-1",
            "schema_digest": "schema-1",
            "ready_capabilities": [],
        })
        return FakeProcess()

    monkeypatch.setattr(host_bootstrap, "_read_object", fake_read)
    monkeypatch.setattr(host_bootstrap, "_write_object", fake_write)
    _mock_host_launch(monkeypatch, fake_popen)
    monkeypatch.setattr(host_bootstrap, "_host_birth_identity", lambda _pid: "birth-1")
    # This inventory projection case substitutes OS identity; native command
    # reuse and refusal are exercised by the structured observation cases.
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda state: "ours")
    monkeypatch.setattr(host_bootstrap, "_IDLE_CHECK_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(host_bootstrap, "_retire_host_locked", lambda current: terminated.append(dict(current)))

    if windows_acl is False:
        with pytest.raises(host_bootstrap.PackHostBootstrapError, match="handoff is unavailable"):
            host_bootstrap.ensure_pack_host(value, reconfigure_action="reconfigure")
        assert launches == []
        return
    result = host_bootstrap.ensure_pack_host(value, reconfigure_action="reconfigure")
    assert result["host_status"] == "ready"
    assert result["host_source_inventory_identity"] == "inventory-1"
    assert len(inventory_calls) == 1
    assert "--source-inventory-identity" in launches[0]
    assert launches[0][launches[0].index("--source-inventory-identity") + 1] == "inventory-1"
    assert launches[0][launches[0].index("--runtime-discovery-file") + 1] == str(
        credential.parent.parent / "discovery.json"
    )
    assert launches[0].count("--pack-root") == 2
    # The runtime-issued handoff (worker credential, least-privilege actor,
    # runtime instance, epoch and schema) is what the registered host binds to.
    assert state["endpoint"] == value["endpoint"]
    assert state["credential_file"] == str(credential)
    assert state["executor_id"] == host_bootstrap.PACK_HOST_ACTOR
    assert state["runtime_instance_id"] == value["runtime_instance_id"]
    assert state["runtime_epoch"] == value["runtime_epoch"]
    assert state["schema_digest"] == value["schema_digest"]
    assert state["source_checkout"] == str(source)

    inventory.identity = "inventory-2"
    ready.clear()
    result2 = host_bootstrap.ensure_pack_host(value, reconfigure_action="reconfigure")
    assert result2["host_status"] == "ready"
    assert len(launches) == 2
    assert "launch_argv" not in state
    assert len(inventory_calls) == 2
    assert terminated, "changed source inventory must not reuse the old ready host"

    # Disabling the last managed source must not reuse a host that still
    # advertises the previously selected nonempty inventory.
    inventory.identity = ""
    inventory.sources = ()
    inventory.roots = ()
    result3 = host_bootstrap.ensure_pack_host(value, reconfigure_action="reconfigure")
    assert result3["host_status"] == "ready"
    assert len(launches) == 3
    assert len(inventory_calls) == 3


def test_unresolved_remotion_probe_blocks_host_spawn(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "astrid" / "packs").mkdir(parents=True)
    _materialize_source_closure(source)
    credential = tmp_path / "support" / "credentials" / "worker.token"
    credential.parent.mkdir(parents=True)
    credential.write_text("worker", encoding="utf-8")
    credential.chmod(0o600)
    value = {
        "worker_credential_file": str(credential),
        "source_checkout": str(source),
        "worker_actor": host_bootstrap.PACK_HOST_ACTOR,
        "worker_scopes": list(host_bootstrap.PACK_HOST_SCOPES),
        "endpoint": "http://runtime.test",
        "runtime_epoch": 1,
        "runtime_instance_id": "instance-1",
        "schema_digest": "schema-1",
    }
    monkeypatch.setattr(
        "astrid.core.pack.source_setup.active_source_inventory",
        lambda: SimpleNamespace(identity="", roots=(), sources=()),
    )
    from astrid.core.execution import generic_host

    class FakeRuntimeClient:
        def __init__(self, endpoint, token):
            assert endpoint == "http://runtime.test"
            assert token == "worker"

        def health(self):
            return {
                "status": "ok", "runtime_epoch": 1,
                "runtime_instance_id": "instance-1", "schema_digest": "schema-1",
            }

    monkeypatch.setattr(generic_host, "RuntimeProtocolClient", FakeRuntimeClient)
    monkeypatch.setattr(host_bootstrap, "_read_object", lambda _path: None)
    monkeypatch.setattr(host_bootstrap, "_host_render_environment", lambda _source: {})
    launches: list[list[str]] = []

    def record_launch(argv, **_kwargs):
        launches.append(list(argv))
        pytest.fail("host was spawned despite unresolved probe custody")

    _mock_host_launch(monkeypatch, record_launch)
    from astrid.core.rendering import remotion_runtime

    retained = SimpleNamespace(pid=5151)
    fatal_probe = remotion_runtime.RemotionProbeCleanupError(
        "trusted Remotion probe cleanup is unproved"
    )
    fatal_probe.process = retained
    fatal_probe.cleanup_error = RuntimeError("simulated live probe child")
    monkeypatch.setattr(
        remotion_runtime,
        "remotion_runtime_status",
        lambda **_kwargs: (_ for _ in ()).throw(fatal_probe),
    )

    with pytest.raises(
        host_bootstrap.PackHostBootstrapError, match="pack host launch is blocked"
    ):
        host_bootstrap.ensure_pack_host(value, reconfigure_action="reconfigure")
    assert launches == []


def test_bootstrap_stops_on_correlated_terminal_registration_failure(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    (source / "astrid" / "packs").mkdir(parents=True)
    (source / "astrid" / "packs" / "marker.txt").write_text(
        "pack", encoding="utf-8"
    )
    _materialize_source_closure(source)
    support = tmp_path / "support"
    credentials = support / "credentials"
    credentials.mkdir(parents=True)
    credential = credentials / "worker.token"
    credential.write_text("worker", encoding="utf-8")
    os.chmod(credential, 0o600)
    value = {
        "worker_credential_file": str(credential),
        "source_checkout": str(source),
        "worker_actor": host_bootstrap.PACK_HOST_ACTOR,
        "worker_scopes": list(host_bootstrap.PACK_HOST_SCOPES),
        "endpoint": "http://runtime.test",
        "runtime_epoch": 1,
        "runtime_instance_id": "instance-1",
        "schema_digest": "schema-1",
    }
    inventory = SimpleNamespace(identity="", roots=(), sources=())
    monkeypatch.setattr(
        "astrid.core.pack.source_setup.active_source_inventory", lambda: inventory
    )

    from astrid.core.execution import generic_host

    class FakeRuntimeClient:
        def __init__(self, endpoint, credential):
            assert endpoint == "http://runtime.test"
            assert credential == "worker"

        def health(self):
            return {
                "status": "ok",
                "runtime_epoch": 1,
                "runtime_instance_id": "instance-1",
                "schema_digest": "schema-1",
            }

    monkeypatch.setattr(generic_host, "RuntimeProtocolClient", FakeRuntimeClient)
    ready: dict[str, object] = {}
    launches: list[list[str]] = []
    terminated: list[dict[str, object]] = []

    class FakeProcess:
        pid = 4242

        def poll(self):
            return None

    def fake_read(path: Path):
        if path.name == "generic-host.ready.json":
            return ready or None
        return None

    def fake_popen(argv, **_kwargs):
        launches.append(list(argv))
        ready.update(
            {
                "status": "failed",
                "terminal": True,
                "pid": 4242,
                "process_birth_id": "birth-failed",
                "error": {
                    "code": "registration_unavailable",
                    "request_id": "request-bootstrap-1",
                    "message": "executor registration unavailable",
                },
            }
        )
        return FakeProcess()

    monkeypatch.setattr(host_bootstrap, "_read_object", fake_read)
    _mock_host_launch(monkeypatch, fake_popen)
    monkeypatch.setattr(
        host_bootstrap, "_host_birth_identity", lambda _pid: "birth-failed"
    )
    monkeypatch.setattr(
        host_bootstrap,
        "_terminate_old_host",
        lambda current: terminated.append(dict(current)),
    )

    with pytest.raises(host_bootstrap.PackHostBootstrapError) as caught:
        host_bootstrap.ensure_pack_host(value, reconfigure_action="reconfigure")

    assert caught.value.code == "registration_unavailable"
    assert caught.value.request_id == "request-bootstrap-1"
    assert caught.value.terminal is True
    assert "request-bootstrap-1" in str(caught.value)
    assert len(launches) == 1
    assert len(terminated) == 1


def test_bootstrap_refuses_runtime_health_without_ok_status(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    (source / "astrid" / "packs").mkdir(parents=True)
    _materialize_source_closure(source)
    credentials = tmp_path / "support" / "credentials"
    credentials.mkdir(parents=True)
    credential = credentials / "worker.token"
    credential.write_text("worker", encoding="utf-8")
    os.chmod(credential, 0o600)
    value = {
        "worker_credential_file": str(credential),
        "source_checkout": str(source),
        "worker_actor": host_bootstrap.PACK_HOST_ACTOR,
        "worker_scopes": list(host_bootstrap.PACK_HOST_SCOPES),
        "endpoint": "http://runtime.test",
    }
    monkeypatch.setattr(
        "astrid.core.pack.source_setup.active_source_inventory",
        lambda: SimpleNamespace(identity="", roots=(), sources=()),
    )
    from astrid.core.execution import generic_host

    class UnhealthyRuntime:
        def __init__(self, *_args):
            pass

        def health(self):
            return {
                "status": "unhealthy",
                "runtime_epoch": 1,
                "runtime_instance_id": "instance-1",
                "schema_digest": "schema-1",
            }

    monkeypatch.setattr(generic_host, "RuntimeProtocolClient", UnhealthyRuntime)
    _mock_host_launch(
        monkeypatch,
        lambda *_args, **_kwargs: pytest.fail("unhealthy runtime launched pack host"),
    )

    with pytest.raises(host_bootstrap.PackHostBootstrapError) as caught:
        host_bootstrap.ensure_pack_host(value, reconfigure_action="reconfigure")

    assert caught.value.code == "runtime_not_ready"
    assert caught.value.terminal is True



def _owned_registration(tmp_path, monkeypatch, process):
    support = tmp_path / "runtime"
    support.mkdir()
    state_path, ready_path = support / "generic-host.json", support / "generic-host.ready.json"
    state = {
        "pid": process.pid, "process_birth_id": "original-birth",
        "runtime_instance_id": "original-instance", "runtime_epoch": 3,
        "boot_manifest_hash": "original-boot", "executor_id": "astrid-pack-host",
        "support_root": str(support), "ready_file": str(ready_path),
        "source_checkout": "/selected/installed", "source_closure_digest": "original-source",
        "schema_digest": "original-schema",
    }
    state_path.write_text(json.dumps(state))
    ready_path.write_text(json.dumps({**state, "status": "ready"}))
    process._astrid_host_registration = (state_path, ready_path, dict(state))
    monkeypatch.setitem(host_bootstrap._RETAINED_HOSTS, (process.pid, state["process_birth_id"]), process)
    return state, state_path, ready_path



def test_installed_profile_strips_source_pythonpath_and_probes_selected_interpreter(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTRID_HOST_EXECUTION_PROFILE", "installed")
    monkeypatch.setenv("PYTHONPATH", "/foreign/checkout")
    seen = []
    monkeypatch.setattr(host_bootstrap, "_installed_execution_origins", lambda root, python: seen.append((root, python)) or {"host": str(root / "astrid/core/execution/generic_host.py")})
    monkeypatch.setattr(host_bootstrap, "_provision_render_runtime_env", lambda *_args: None)
    env = host_bootstrap._host_render_environment(tmp_path)
    assert "PYTHONPATH" not in env
    assert seen == [(tmp_path, host_bootstrap._pack_host_python_executable())]


def test_installed_profile_refuses_checkout_origins_from_isolated_probe(monkeypatch, tmp_path):
    seen = []
    def probe(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps({key: "/foreign/source.py" for key in ["host", "child", "action", "client", "sdk"]}), "")
    monkeypatch.setattr(host_bootstrap.subprocess, "run", probe)
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="actual installed"):
        host_bootstrap._installed_execution_origins(tmp_path, "/selected/python")
    assert seen[0][0][:4] == ["/selected/python", "-X", "utf8", "-I"]
    assert seen[0][0][4] == "-c"
    assert "PYTHONPATH" not in seen[0][1]["env"]
    assert seen[0][1]["cwd"] != str(tmp_path)


def test_installed_profile_launch_preserves_identity_and_uses_isolated_interpreter(monkeypatch, tmp_path):
    # Reuse the existing exact source/inventory/ready-binding fixture while
    # replacing only the independent installed-origin probe dependency.
    monkeypatch.setenv("ASTRID_HOST_EXECUTION_PROFILE", "installed")
    monkeypatch.setattr(host_bootstrap, "_installed_execution_origins", lambda root, python: {"host": str(root / "astrid/core/execution/generic_host.py")})
    monkeypatch.setattr(host_bootstrap, "_provision_render_runtime_env", lambda *_: None)
    original = _mock_host_launch
    launches = []
    def isolate(monkeypatch, fake_popen):
        def guarded(argv, **kwargs):
            launches.append(argv)
            assert argv[1:5] == ["-X", "utf8", "-I", "-m"]
            assert "PYTHONPATH" not in kwargs["env"]
            assert kwargs["cwd"] != str(tmp_path / "source")
            return fake_popen(argv, **kwargs)
        return original(monkeypatch, guarded)
    monkeypatch.setitem(globals(), "_mock_host_launch", isolate)
    test_bootstrap_passes_inventory_identity_and_restarts_on_change(monkeypatch, tmp_path, windows_acl=None)
    assert len(launches) == 3


@pytest.mark.parametrize("profile", ["source", "installed"])
def test_parked_launch_diagnostics_preserve_exact_ready_identity(monkeypatch, tmp_path, profile):
    from astrid.core.execution import generic_host
    source = tmp_path / "source"; source.mkdir()
    support = tmp_path / "runtime"; support.mkdir()
    credential = support / "credential"; credential.write_text("credential")
    monkeypatch.setenv("ASTRID_HOST_EXECUTION_PROFILE", profile)
    origins = {"host": str(source / "astrid/core/execution/generic_host.py")}
    monkeypatch.setattr(host_bootstrap, "_installed_execution_origins", lambda *_: origins)
    monkeypatch.setattr(host_bootstrap, "_dependency_pythonpath", lambda: [])
    monkeypatch.setattr(host_bootstrap, "_provision_render_runtime_env", lambda *_: None)
    monkeypatch.setattr(host_bootstrap, "_host_birth_identity", lambda *_: "birth")
    monkeypatch.setattr(host_bootstrap, "_capture_native_host_launch_identity", lambda pid: {
        "pid": pid, "uid": os.getuid(), "pgid": pid, "start_sec": 1700000000, "start_usec": 1})
    monkeypatch.setattr(host_bootstrap, "_parked_host_identity_matches", lambda *_: True)
    process = SimpleNamespace(pid=7654321, poll=lambda: None)
    monkeypatch.setattr(host_bootstrap, "popen_owned_group", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(generic_host, "RuntimeProtocolClient", lambda *_: object())
    monkeypatch.setattr(generic_host, "_await_enabled_runtime_credential", lambda *_args, **_kwargs: None)
    reference = SimpleNamespace(effective_target={"kind": "machine", "id": host_bootstrap.socket.gethostname()},
        source_checkout=source, pack_roots=(source,), ready_file=support / "selected.ready.json",
        digest=lambda: "deployment", dependency_closure=(), support_root=support,
        credential_ref="file:" + str(credential), runtime_endpoint="http://runtime.test",
        source_closure_digest="sha256:closure", boot_manifest_path=source / "boot.json",
        boot_manifest_hash="sha256:boot", executor_id="executor")
    launch = SimpleNamespace(deployment_digest="deployment", env=lambda: {},
                             argv=("/selected/python", "-m", "astrid.core.execution.generic_host"), cwd=str(source))
    preparer = host_bootstrap._ParkedPackHostPreparer(lambda _: launch)
    handle = preparer.prepare(reference)
    try:
        assert handle.state["launch_argv_sha256"] == host_bootstrap._launch_argv_digest(process._astrid_host_launch_argv)
        assert "launch_argv" not in handle.state
        assert "--activation-fd" in process._astrid_host_launch_argv
        assert handle.execution_profile == profile
        assert handle.execution_origins == (origins if profile == "installed" else {})
        assert "execution_profile" not in handle.state and "execution_origins" not in handle.state
        ready = {**handle.state, "status": "ready", "executor_id": "executor"}
        reference.ready_file.write_text(json.dumps(ready))
        preparer.await_ready(handle)
        ready["source_closure_digest"] = "foreign"
        reference.ready_file.write_text(json.dumps(ready))
        with pytest.raises(host_bootstrap.PackHostBootstrapError, match="foreign"):
            preparer.await_ready(handle)
    finally:
        handle.control.close()
        host_bootstrap._RETAINED_HOSTS.pop((process.pid, "birth"), None)


@pytest.mark.parametrize("budget", [0, -1, True, False, float("nan"), float("inf"), -float("inf"), "120", None])
def test_startup_budget_rejected_before_handoff_side_effects(monkeypatch, budget):
    class NoRead(dict):
        def get(self, *args):
            pytest.fail("invalid budget reached credential/handoff access")
    monkeypatch.setattr(host_bootstrap, "popen_owned_group", lambda *_args, **_kw: pytest.fail("invalid budget launched a host"))
    with pytest.raises(ValueError, match="finite, positive"):
        host_bootstrap.ensure_pack_host(NoRead(), reconfigure_action="repair", startup_idle_seconds=budget)


@pytest.mark.parametrize("budget,ready_at,mode,success,elapsed,progress_until", [
    (None, 25, "ready", True, 25, None),
    (None, None, "ready", False, host_bootstrap.HOST_STARTUP_IDLE_SECONDS, None),
    (120, 25, "ready", True, 25, None),
    (120, None, "ready", False, 120, None),
    (120, 1, "terminal", False, 1, None),
    (120, 1, "foreign_pid", False, 1, None),
    (120, 1, "foreign_birth", False, 1, None),
    (120, 1, "foreign_instance", False, 1, None),
    (120, 1, "foreign_source", False, 1, None),
    (120, None, "exit", False, 1, None),
    # Alive and progressing well past the idle window is still waited for.
    (120, 400, "ready", True, 400, 400),
    # Progress resets the idle deadline; silence afterwards fails as hung.
    (120, None, "ready", False, 150.3, 30),
    (120, None, "exit", False, 1, 30),
])
def test_startup_budget_polling_preserves_identity_and_original_cleanup(
    monkeypatch, tmp_path, budget, ready_at, mode, success, elapsed, progress_until,
):
    import time as real_time
    from astrid.core.execution import generic_host
    source = tmp_path / "source"
    (source / "astrid/packs").mkdir(parents=True)
    _materialize_source_closure(source)
    support = tmp_path / "support"
    credential = support / "credentials/worker.token"
    credential.parent.mkdir(parents=True)
    credential.write_text("fixture-worker")
    credential.chmod(0o600)
    value = {"worker_credential_file": str(credential), "source_checkout": str(source),
             "worker_actor": host_bootstrap.PACK_HOST_ACTOR,
             "worker_scopes": list(host_bootstrap.PACK_HOST_SCOPES),
             "endpoint": "http://runtime.test", "runtime_epoch": 1,
             "runtime_instance_id": "instance", "schema_digest": "schema"}
    monkeypatch.setenv("ASTRID_HOST_EXECUTION_PROFILE", "source")
    monkeypatch.setattr("astrid.core.pack.source_setup.active_source_inventory", lambda: SimpleNamespace(identity="", roots=(), sources=()))
    class Client:
        def __init__(self, endpoint, token):
            assert endpoint == value["endpoint"] and token == "fixture-worker"
        def health(self):
            return {"status": "ok", "runtime_epoch": 1, "runtime_instance_id": "instance", "schema_digest": "schema"}
    monkeypatch.setattr(generic_host, "RuntimeProtocolClient", Client)
    clock = [0.0]
    def sleep(seconds): clock[0] += seconds
    monkeypatch.setattr(host_bootstrap, "time", SimpleNamespace(**{**vars(real_time), "monotonic": lambda: clock[0], "sleep": sleep}))
    payload = {}; launched = []; cleanup = []
    class OriginalProcess:
        pid = 54321
        returncode = None
        def poll(self):
            if mode == "exit" and clock[0] >= 1: self.returncode = 1
            return self.returncode
        def wait(self, timeout=None):
            cleanup.append("original_wait")
            return self.returncode
    process = OriginalProcess()
    def launch(argv, **kwargs):
        launched.append(process)
        def option(name): return argv[argv.index(name) + 1]
        payload.update(status="ready", pid=process.pid, process_birth_id="birth",
                       python_executable=argv[0], endpoint=option("--runtime-endpoint"),
                       executor_id=option("--executor-id"), ready_file=option("--ready-file"),
                       credential_file=option("--credential-file"), support_root=option("--support-root"),
                       source_checkout=option("--source-checkout"),
                       source_checkout_digest=option("--source-checkout-digest"),
                       source_closure_digest=option("--source-closure-digest"),
                       source_inventory_identity=option("--source-inventory-identity"),
                       runtime_instance_id="instance", runtime_epoch=1, schema_digest="schema",
                       boot_manifest_path=option("--boot-manifest-path"), boot_manifest_hash=option("--boot-manifest-hash"))
        if mode == "terminal": payload.update(status="failed", terminal=True, error={"code": "registration_unavailable", "request_id": "fixture-request", "message": "fixture failure"})
        elif mode == "foreign_pid": payload["pid"] += 1
        elif mode == "foreign_birth": payload["process_birth_id"] = "foreign"
        elif mode == "foreign_instance": payload["runtime_instance_id"] = "foreign"
        elif mode == "foreign_source": payload["source_closure_digest"] = "foreign"
        return process
    def read(path):
        if path.name == "generic-host.ready.json" and launched and ready_at is not None and clock[0] >= ready_at: return payload
        return None
    monkeypatch.setattr(host_bootstrap, "_RETAINED_HOSTS", {})
    monkeypatch.setattr(host_bootstrap, "_read_object", read)
    monkeypatch.setattr(host_bootstrap, "_host_birth_identity", lambda _pid: "birth")
    # A host that fails to register is retired by its registration.
    monkeypatch.setattr(host_bootstrap, "_retire_host_locked", lambda state: cleanup.append("retired"))
    _mock_host_launch(monkeypatch, launch)
    # Injected progress source: advances while the host is "making progress".
    monkeypatch.setattr(host_bootstrap, "_host_progress_token",
                        lambda *_a, **_k: ("tick", int(clock[0])) if progress_until and clock[0] < progress_until else "static")
    options = {} if budget is None else {"startup_idle_seconds": budget}
    if success:
        result = host_bootstrap.ensure_pack_host(value, reconfigure_action="repair", **options)
        assert result["host_status"] == "ready" and not cleanup
    else:
        with pytest.raises(host_bootstrap.PackHostBootstrapError) as caught:
            host_bootstrap.ensure_pack_host(value, reconfigure_action="repair", **options)
        assert cleanup == ["retired"]
        if mode == "exit":
            assert "exited with status 1" in str(caught.value)
        if mode == "ready" and success is False and budget is not None:
            assert f"no progress for {budget:g}s" in str(caught.value) and "waiting for host ready file" in str(caught.value)
        if mode == "terminal":
            assert caught.value.code == "registration_unavailable" and caught.value.request_id == "fixture-request" and caught.value.terminal
    assert launched == [process]
    assert clock[0] == pytest.approx(elapsed, abs=0.051)



@pytest.fixture
def native_host_command_fixture(monkeypatch, tmp_path):
    from astrid.core.execution import custody_broker as broker
    executable = tmp_path / "python"
    executable.write_text("fixture")
    argv = [str(executable), "-I", "-m", "astrid.core.execution.generic_host", "run",
            "--credential-file", str(tmp_path / "worker.token"), "--max-concurrency", "2",
            "--runtime-endpoint", "http://runtime.test", "--source-checkout", str(tmp_path / "source"),
            "--boot-manifest-hash", "sha256:" + "a" * 64]
    row = {"pid": 4242, "uid": os.getuid(), "parent_pid": 8, "pgid": 4242,
           "status": 2, "start_sec": 1700000000, "start_usec": 1234}
    state = {"pid": row["pid"], "process_birth_id": broker._ps_lstart_birth(row["start_sec"]),
             "launch_argv_sha256": host_bootstrap._launch_argv_digest(argv),
             "launch_native_identity": {k: row[k] for k in ("pid", "uid", "pgid", "start_sec", "start_usec")}}
    process = SimpleNamespace(_astrid_host_launch_argv=tuple(argv))
    def metadata(values):
        raw = (len(values).to_bytes(4, sys.byteorder, signed=True) + os.fsencode(values[0]) + b"\0\0"
               + b"\0".join(os.fsencode(value) for value in values) + b"\0")
        return {"executable": values[0], "bsd_identity": {k: row[k] for k in
                ("pid", "uid", "parent_pid", "start_sec", "start_usec")}, **broker._diagnostic_argv(raw)}
    import sys
    monkeypatch.setattr(host_bootstrap.sys, "platform", "darwin")
    monkeypatch.setattr(broker, "_darwin_process_records", lambda pid: [dict(row)])
    monkeypatch.setattr(broker, "_darwin_command_metadata", lambda pid: metadata(argv))
    monkeypatch.setitem(host_bootstrap._RETAINED_HOSTS, (4242, state["process_birth_id"]), process)
    def forbidden(*args, **kwargs): raise AssertionError("native observation must not launch ps")
    monkeypatch.setattr(host_bootstrap.os, "popen", forbidden)
    # Exact digest matching must not execute the optional framework verifier.
    monkeypatch.setattr(host_bootstrap, "subprocess", SimpleNamespace(**{**vars(subprocess), "run": forbidden}))
    return SimpleNamespace(row=row, argv=argv, state=state, process=process, broker=broker,
                           metadata=metadata, executable=executable)


@pytest.mark.parametrize("variant", ["source", "installed", "parked"])
def test_darwin_host_reuse_matches_full_known_launch_without_ps(native_host_command_fixture, variant):
    f = native_host_command_fixture
    if variant == "source": f.argv.remove("-I")
    elif variant == "parked": f.argv.extend(["--activation-fd", "7", "--activation-operation-id", "operation",
                                             "--activation-channel-id", "channel"])
    f.process._astrid_host_launch_argv = tuple(f.argv)
    f.state["launch_argv_sha256"] = host_bootstrap._launch_argv_digest(f.argv)
    assert host_bootstrap._our_host(4242) is True
    assert host_bootstrap._parked_host_identity_matches(f.state) is True
    observed = host_bootstrap._darwin_host_command_observation(4242, f.state)
    assert "<redacted>" in observed["metadata_first"]["argv"]
    assert observed["metadata_first"]["argv_sha256"] == f.state["launch_argv_sha256"]


@pytest.mark.parametrize("fault", ["missing-pin", "credential-tail", "parent", "uid", "group", "birth",
                                  "zombie", "census", "pid-reuse", "opaque-argv", "prior-same-second-reuse", "missing-precise-birth", "endpoint", "source", "boot", "bsd-mismatch"])
def test_darwin_host_unknown_or_changed_command_blocks_before_retirement(native_host_command_fixture, monkeypatch, fault):
    f = native_host_command_fixture
    retire = []
    monkeypatch.setattr(host_bootstrap, "_retire_host_locked", lambda state: retire.append(state))
    if fault == "missing-pin": del f.state["launch_argv_sha256"]
    elif fault == "credential-tail": f.argv[f.argv.index("--credential-file") + 1] += "-other"
    elif fault in {"endpoint", "source", "boot"}:
        flag = {"endpoint": "--runtime-endpoint", "source": "--source-checkout", "boot": "--boot-manifest-hash"}[fault]
        f.argv[f.argv.index(flag) + 1] += "-other"
    elif fault == "bsd-mismatch":
        def mismatched(pid):
            value = f.metadata(f.argv)
            value["bsd_identity"]["start_usec"] += 1
            return value
        monkeypatch.setattr(f.broker, "_darwin_command_metadata", mismatched)
    elif fault == "parent":
        first = dict(f.row)
        n = [0]
        def rows(pid):
            n[0] += 1
            return [{**first, "parent_pid": 8 if n[0] == 1 else 9}]
        monkeypatch.setattr(f.broker, "_darwin_process_records", rows)
    elif fault == "uid": f.row["uid"] += 1
    elif fault == "group": f.row["pgid"] += 1
    elif fault == "birth": f.row["start_sec"] += 1
    elif fault == "prior-same-second-reuse": f.row["start_usec"] += 1
    elif fault == "missing-precise-birth": del f.state["launch_native_identity"]
    elif fault == "zombie": f.row["status"] = 5
    elif fault == "census":
        def unavailable(pid): raise OSError("native census unavailable")
        monkeypatch.setattr(f.broker, "_darwin_process_records", unavailable)
    elif fault == "pid-reuse":
        n = [0]
        def rows(pid):
            n[0] += 1
            return [{**f.row, "start_usec": 1234 if n[0] == 1 else 1235}]
        monkeypatch.setattr(f.broker, "_darwin_process_records", rows)
    else:
        monkeypatch.setattr(f.broker, "_darwin_command_metadata", lambda pid: {
            "executable": str(f.executable), "bsd_identity": {k: f.row[k] for k in
            ("pid", "uid", "parent_pid", "start_sec", "start_usec")}, "argv_outcome": "UNAVAILABLE_OR_AMBIGUOUS"})
    with pytest.raises(host_bootstrap.PackHostBootstrapError):
        # This is the exact decision ordering at ensure_pack_host's retirement boundary.
        if host_bootstrap._parked_host_identity_matches(f.state):
            host_bootstrap._retire_host_locked(f.state)
    assert retire == []


def test_darwin_framework_variant_requires_verified_executable_and_exact_private_tail(native_host_command_fixture, monkeypatch):
    f = native_host_command_fixture
    actual = f.executable.with_name("Python")
    actual.write_text("fixture")
    f.argv[0] = str(actual)
    probes = []
    def selected_probe(argv, **kwargs):
        probes.append(argv)
        return SimpleNamespace(stdout=str(actual))
    monkeypatch.setattr(host_bootstrap, "subprocess", SimpleNamespace(**{**vars(subprocess), "run": selected_probe}))
    assert host_bootstrap._parked_host_identity_matches(f.state) is True
    assert probes[0][0] == str(f.executable)
    assert probes[0][1:5] == ["-X", "utf8", "-I", "-S"]
    f.argv[f.argv.index("--max-concurrency") + 1] = "3"
    with pytest.raises(host_bootstrap.PackHostBootstrapError):
        host_bootstrap._parked_host_identity_matches(f.state)
    del f.process._astrid_host_launch_argv
    with pytest.raises(host_bootstrap.PackHostBootstrapError):
        host_bootstrap._parked_host_identity_matches(f.state)


@pytest.mark.parametrize("argv", [["python", "--max-concurrency"],
                                  ["python", "--max-concurrency", "2", "--max-concurrency", "2"],
                                  ["python", "--max-concurrency", "0"]])
def test_launch_capacity_is_validated_before_pin(argv):
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="capacity"):
        host_bootstrap._launch_argv_digest(argv)



def test_parked_inspector_rejects_capacity_drift_after_full_native_identity(native_host_command_fixture, monkeypatch, tmp_path):
    from astrid.core.execution.process_group import _ProcessInfo
    f = native_host_command_fixture
    f.argv[f.argv.index("--max-concurrency") + 1] = "3"
    f.process._astrid_host_launch_argv = tuple(f.argv)
    f.state["launch_argv_sha256"] = host_bootstrap._launch_argv_digest(f.argv)
    f.process.pid = 4242
    reference = SimpleNamespace(executable=SimpleNamespace(path=f.executable,
        digest=host_bootstrap._host_artifact_digest(f.executable)), capacity=2)
    handle = host_bootstrap._ParkedPackHostHandle(f.process, None, reference, f.state, "operation", "channel")
    row = _ProcessInfo(4242, 8, 4242, f.state["process_birth_id"], status=2, start_sec=1700000000,
                       start_usec=1234, uid=os.getuid())
    monkeypatch.setattr(host_bootstrap, "_process_snapshot", lambda: {4242: row})
    inspector = host_bootstrap._ParkedPackHostInspector(lambda: {}, session_config_path=tmp_path / "session.json")
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="capacity changed"):
        inspector.observe(handle)


@pytest.mark.parametrize("outcome", ["live", "absent", "zombie", "unavailable"])
@pytest.mark.posix_native  # POSIX-only process-group API
def test_launch_precise_native_identity_is_real_bounded_observation(monkeypatch, outcome):
    from astrid.core.execution import custody_broker
    monkeypatch.setattr(host_bootstrap.sys, "platform", "darwin")
    row = {"pid": 4242, "uid": os.getuid(), "pgid": 4242, "start_sec": 1700000000, "start_usec": 1234,
           "status": 5 if outcome == "zombie" else 2}
    def read(pid):
        if outcome == "unavailable": raise OSError("unavailable")
        return [] if outcome == "absent" else [row]
    monkeypatch.setattr(custody_broker, "_darwin_process_records", read)
    if outcome == "live":
        assert host_bootstrap._capture_native_host_launch_identity(4242) == {k: row[k] for k in
            ("pid", "uid", "pgid", "start_sec", "start_usec")}
    else:
        with pytest.raises(host_bootstrap.PackHostBootstrapError, match="precise launch identity unavailable"):
            host_bootstrap._capture_native_host_launch_identity(4242)


@pytest.mark.posix_native  # POSIX-only process-group API
def test_non_darwin_host_identity_keeps_existing_command_contract(monkeypatch):
    monkeypatch.setattr(host_bootstrap.sys, "platform", "linux")
    state = {"pid": 4242, "process_birth_id": "birth", "ready_file": "/ready", "source_checkout": "/source",
             "credential_file": "/credential", "support_root": "/support", "endpoint": "http://runtime",
             "source_closure_digest": "closure", "boot_manifest_path": "/manifest", "boot_manifest_hash": "hash"}
    monkeypatch.setattr(host_bootstrap, "_host_pid_alive", lambda pid: True)
    monkeypatch.setattr(host_bootstrap, "_host_birth_identity", lambda pid: "birth")
    monkeypatch.setattr(host_bootstrap.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(host_bootstrap, "_host_command", lambda pid: " ".join([
        "astrid.core.execution.generic_host", *[str(v) for k, v in state.items() if k not in {"pid", "process_birth_id"}]]))
    assert host_bootstrap._parked_host_identity_matches(state)


def _framework_native_fixture(f, monkeypatch):
    actual = f.executable.with_name("Python")
    actual.write_text("fixture")
    f.argv[0] = str(actual)
    f.process.pid = f.state["pid"]
    f.state["python_executable"] = str(f.executable)
    monkeypatch.setattr(host_bootstrap, "_host_executable_matches",
                        lambda observed, selected: observed == str(actual) and selected == f.executable)
    return actual



@pytest.mark.parametrize(
    "overrides",
    [
        {"worker_actor": "someone-else"},
        {"worker_scopes": []},
        {"worker_scopes": list(host_bootstrap.PACK_HOST_SCOPES)[:-1]},
        {"worker_scopes": [*host_bootstrap.PACK_HOST_SCOPES, "extra:scope"]},
    ],
)
def test_bootstrap_refuses_worker_credential_outside_the_pack_host_contract(
    monkeypatch, tmp_path: Path, overrides: dict
) -> None:
    source = tmp_path / "source"
    (source / "astrid" / "packs").mkdir(parents=True)
    _materialize_source_closure(source)
    support = tmp_path / "support" / "nested"
    support.mkdir(parents=True)
    credential = support.parent / "worker.token"
    credential.write_text("worker", encoding="utf-8")
    os.chmod(credential, 0o600)
    inventory = SimpleNamespace(identity="", roots=(), sources=())
    monkeypatch.setattr("astrid.core.pack.source_setup.active_source_inventory", lambda: inventory)
    value = {
        "worker_credential_file": str(credential),
        "source_checkout": str(source),
        "worker_actor": host_bootstrap.PACK_HOST_ACTOR,
        "worker_scopes": list(host_bootstrap.PACK_HOST_SCOPES),
        "endpoint": "http://runtime.test",
        "runtime_epoch": "epoch-1",
        "runtime_instance_id": "instance-1",
        "schema_digest": "schema-1",
        **overrides,
    }

    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="least-privilege pack-host contract"):
        host_bootstrap.ensure_pack_host(value, reconfigure_action="reconfigure")
