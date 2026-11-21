"""Repair a real Runtime when its declared service supervisor is unhealthy."""

import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

import pytest

import runtime_protocol
from astrid.installer.backend import Backend
from astrid.installer.core import Installer, VerificationError, Version, current, write_json
from runtime_protocol import platform


def wait_for_service(backend, version, data, expected, timeout=20):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = backend.runtime(version, data, "status").get("health", {})
        states = {row.get("name"): row.get("state") for row in latest.get("services", [])}
        if states.get("pack-host") == expected:
            return latest
        time.sleep(0.1)
    raise AssertionError(f"Runtime service did not reach {expected}: {latest}")


@pytest.mark.parametrize(("fail_log_open", "port_offset"), [(False, 0), (True, 1)])
def test_repair_restarts_real_runtime_for_failed_service_and_preserves_healthy_pids(
    tmp_path, monkeypatch, fail_log_open, port_offset,
):
    port = int(os.environ.get("ASTRID_TEST_INSTALLER_REPAIR_PORT", "17545")) + port_offset
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "local-app-data"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg-config"))
    monkeypatch.setenv("ASTRID_RUNTIME_DOOR_PORT", str(port))

    root, data = tmp_path / "program", tmp_path / "workspace"
    version_root = root / "versions" / "v1"
    version_root.mkdir(parents=True)
    python = Path(sys.executable).absolute()
    source_checkout = Path(__file__).resolve().parents[1]
    runtime_checkout = Path(runtime_protocol.__file__).resolve().parent.parent
    realm_id = str(uuid.uuid4())
    realm_root = data / "runtime" / "realms" / realm_id
    pid_file = data / "pack-host-service.pid"
    ready_file = data / "runtime" / "generic-host.ready.json"
    service_script = tmp_path / "pack_host_service.py"
    service_script.write_text(
        "import os, pathlib, time\n"
        "pathlib.Path(os.environ['PROBE_PID_FILE']).write_text(str(os.getpid()))\n"
        "ready = pathlib.Path(os.environ['READY_FILE'])\n"
        "ready.parent.mkdir(parents=True, exist_ok=True)\n"
        "ready.write_text('{}')\n"
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    services = tmp_path / "services.json"
    write_json(services, {"version": 1, "services": [{
        "name": "pack-host", "argv": [str(python), str(service_script)],
        "env": {"PROBE_PID_FILE": str(pid_file), "READY_FILE": str(ready_file)},
        "restart": "always",
        "ready": {"file": "generic-host.ready.json", "timeout_seconds": 5},
    }]})
    profile = tmp_path / "profile.json"
    write_json(profile, {
        "profile": "astrid", "runtime_checkout": str(runtime_checkout),
        "source_checkout": str(source_checkout), "service_declaration": str(services),
    })
    version = Version(version_root, python, profile, {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "ASTRID_RUNTIME_DOOR_PORT": str(port),
        "ASTRID_RUNTIME_DATA_ROOT": str(data),
    }, release=1)
    write_json(version_root / "version.json", version.document())
    (root / "current").write_text("v1\n", encoding="utf-8")

    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv")
    backend.service.system = "linux"
    backend.service.systemd = lambda: False
    backend.open = lambda: None
    started = False
    try:
        backend.runtime(version, data, "workspace", "create", "--realm-root", str(realm_root),
                        "--realm-id", realm_id, "--door-port", str(port),
                        "--hosted-origin", "off", profile=True)
        if fail_log_open:
            # Runtime's real service supervisor fails while opening its log,
            # records that declaration as failed, and keeps the daemon healthy.
            (platform.log_dir() / "service-pack-host.log").mkdir(parents=True)
        backend.register(version, data)
        backend.start(version, data)
        started = True
        initial_health = wait_for_service(backend, version, data, "failed" if fail_log_open else "running")
        initial_discovery = json.loads((data / "runtime" / "discovery.json").read_text(encoding="utf-8"))
        initial_runtime = (initial_discovery["pid"], initial_discovery["process_birth_id"])
        initial_service_pid = int(pid_file.read_text(encoding="utf-8")) if pid_file.exists() else None

        if fail_log_open:
            assert initial_health["status"] == "ok"
            assert initial_health["services"] == [{
                "name": "pack-host", "state": "failed", "last_exit": None,
                "restart_count": 0, "reason": "supervisor_failed",
            }]
            with pytest.raises(VerificationError, match="An Astrid service stopped while starting"):
                backend.verify(version, data, realm_id, timeout=0.5)
            shutil.rmtree(platform.log_dir() / "service-pack-host.log")

        Installer(root, data, backend, say=lambda _line: None).run("install", {})
        post_health = wait_for_service(backend, version, data, "running")
        post_discovery = json.loads((data / "runtime" / "discovery.json").read_text(encoding="utf-8"))
        post_runtime = (post_discovery["pid"], post_discovery["process_birth_id"])
        post_service_pid = int(pid_file.read_text(encoding="utf-8"))
        assert platform.process_identity(post_service_pid) is not None

        if fail_log_open:
            assert post_health["status"] == "ok"
            assert post_runtime != initial_runtime
            assert post_service_pid != initial_service_pid
        else:
            assert post_runtime == initial_runtime
            assert post_service_pid == initial_service_pid
    finally:
        if started:
            backend.stop(current(root), data)
