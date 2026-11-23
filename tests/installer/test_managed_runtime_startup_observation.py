"""Managed startup observation keeps the selected install waiting safely."""
from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer import backend, core, service
from astrid.installer.runtime_lifecycle import managed_runtime_lifecycle


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_managed_up_waits_for_exact_pending_operation_past_legacy_deadline(tmp_path, monkeypatch):
    """Pending startup beyond 90s stays non-ready and never mutates service twice."""
    program, data = tmp_path / "program", tmp_path / "data"
    version_root = program / "versions" / "v13"
    source = version_root / "source"
    source.mkdir(parents=True)
    python = version_root / "py" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    profile = version_root / "profile.json"
    declaration = version_root / "services.json"
    _write(profile, {"source_checkout": str(source), "runtime_environment": str(version_root / "py"),
                     "service_declaration": str(declaration)})
    _write(declaration, {"services": [{"name": "pack-host", "ready": {
        "file": "generic-host.ready.json", "timeout_seconds": 300}}]})
    _write(program / "install.json", {"owner": service.LABEL, "data_root": str(data)})
    selection = tmp_path / "selection.json"
    _write(selection, {"owner": service.LABEL, "program_root": str(program), "data_root": str(data)})
    _write(data / "runtime" / "catalog.json", {"selected_realm_id": "realm-a", "realms": [
        {"realm_id": "realm-a", "data_root": str(data / "realm-a")} ]})
    identity = {"release": 13, "managed_by": None}
    endpoint = "http://127.0.0.1:17340"
    discovery = {"active_realm": "realm-a", "runtime_instance_id": "instance-new",
                 "pid": 4242, "process_birth_id": "birth-new", "door_endpoint": endpoint,
                 "endpoint": "http://127.0.0.1:17341", "identity": identity}
    stale_discovery = {**discovery, "door_endpoint": "http://127.0.0.1:17399"}
    version = core.Version(version_root, python, profile, {"ASTRID_RUNTIME_DOOR_PORT": "17340"}, release=13)
    (data / "runtime" / "generic-host.ready.json").parent.mkdir(parents=True, exist_ok=True)
    (data / "runtime" / "generic-host.ready.json").write_text("{}")
    monkeypatch.setattr(core, "current", lambda _root: version)
    monkeypatch.setattr(service, "install_selection", lambda *_: selection)
    monkeypatch.setattr(service, "installer_lock_path", lambda *_: tmp_path / "installer.lock")
    monkeypatch.setattr(backend.platform, "log_dir", lambda: tmp_path)
    monkeypatch.setattr(backend.platform, "process_identity", lambda _pid: {"birth_id": "birth-new"})

    # The real installer lock remains in use; service-manager and Runtime
    # boundaries below are deterministic and cannot touch a host installation.
    from astrid.installer.app_update import locked
    from astrid.installer import app_update, installation
    monkeypatch.setattr(app_update, "locked", lambda *_a, **_kw: locked(tmp_path / "test.lock"))
    monkeypatch.setattr(installation, "adopt_if_handed_off", lambda *_: None)
    monkeypatch.setattr(backend.Service, "settings", lambda self: None)
    monkeypatch.setattr(backend.Service, "login_service_exists", lambda self: False)
    events = []

    def register(self, *_args):
        events.append("register")

    def start(self, _version, _data):
        events.append("start")
        _write(data / "runtime" / "discovery.json", stale_discovery)

    monkeypatch.setattr(backend.Backend, "register", register)
    monkeypatch.setattr(backend.Backend, "start", start)
    monkeypatch.setattr(backend.Backend, "stop", lambda *_a, **_kw: events.append("stop"))

    class Clock:
        now = 0.0

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(backend.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(backend.time, "sleep", clock.sleep)

    class Response:
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(self.value).encode()

    health = {"status": "ok", "runtime_instance_id": "instance-new", "identity": identity,
              "services": [{"name": "pack-host", "state": "running"}]}

    class Opener:
        opened = []

        def open(self, request, timeout):
            self.opened.append(request)
            assert request.full_url in {endpoint + "/api/runtime/v1/health",
                                        endpoint + "/api/runtime/v1/realm"}
            return Response(health if request.full_url.endswith("/health") else {"realm_id": "realm-a"})

    opener = Opener()
    monkeypatch.setattr(backend.urllib.request, "build_opener", lambda *_args: opener)
    runtime_calls = []
    pending = {"operation_id": "operation-canonical-42", "phase": "starting_service",
               "state": "pending", "objects_checked": 3}

    def runtime(self, selected_version, selected_data, *argv, profile=False, timeout=1800):
        runtime_calls.append((selected_version, selected_data, argv, profile, timeout))
        if argv and argv[0] == "startup-status":
            if clock.now < 91:
                return pending
            _write(data / "runtime" / "discovery.json", discovery)
            return {**pending, "phase": "ready", "state": "completed"}
        assert argv == ("status",)
        observed = discovery if clock.now >= 91 else stale_discovery
        return {"discovery": observed, "support": {"pid_alive": True}}

    monkeypatch.setattr(backend.Backend, "runtime", runtime)

    result = managed_runtime_lifecycle(action="up", source_checkout=source, data_root=data)

    assert clock.now > 90
    assert result["ok"] is False
    assert result["status"] == "starting"
    assert result["operation_id"] == "operation-canonical-42"
    assert events == ["register", "start"]
    observations = [call for call in runtime_calls if call[2] and call[2][0] == "startup-status"]
    assert observations
    expected_observer_args = ("startup-status", "--realm-root", str(data / "realm-a"),
                              "--expected-realm-id", "realm-a", "--require-configured-selection")
    for observed_version, observed_data, argv, profile_flag, command_timeout in observations:
        assert observed_version is version
        assert observed_data == data
        assert argv == expected_observer_args
        assert profile_flag is True
        assert command_timeout == 3
    assert any(call[2] == ("status",) for call in runtime_calls)
    assert {request.full_url for request in opener.opened} == {
        endpoint + "/api/runtime/v1/health", endpoint + "/api/runtime/v1/realm"}
    assert all(request.get_header("Origin") == "https://astrid.haus" for request in opener.opened)
