"""LS-15: reuse is bound to the effective, host-admitted network route."""
from __future__ import annotations

import json
import socket
import subprocess
import sys

from astrid.core.execution import generic_host
from tests.core.execution import test_resident_sessions
from tests.core.execution.test_resident_sessions import run_job

resident_host = test_resident_sessions.resident_host


def test_resident_uses_effective_admitted_route_and_replaces_on_route_change(
    resident_host, monkeypatch,
):
    root, host, runtime = resident_host
    manifest = json.loads((root / "pack.yaml").read_text())
    manifest["actions"]["run"]["metadata"]["network_policy"] = {
        "allowed_protocols": ["tcp"], "allowed_destinations": ["127.0.0.1:8188"],
    }
    (root / "pack.yaml").write_text(json.dumps(manifest))
    host.discover()
    host.register(deliberate=True)
    select = generic_host.open_resident_task
    route = ["127.0.0.1:8199"]

    def admitted(*args, **kwargs):
        kwargs["admission"]["allowed_routes"] = list(route)
        kwargs["effective_policy"] = {
            "allowed_protocols": ["tcp"], "allowed_destinations": list(route),
        }
        return select(*args, **kwargs)

    monkeypatch.setattr(generic_host, "open_resident_task", admitted)
    first = run_job(host, runtime, 1)["action_result"]
    adapter = host.managed_tool_session.current_adapter
    assert adapter.policy["allowed_destinations"] == route
    second = run_job(host, runtime, 2)["action_result"]
    assert second["pid"] == first["pid"]
    assert second["calls"] == 2
    route[:] = ["127.0.0.1:8200"]
    third = run_job(host, runtime, 3)["action_result"]
    assert third["pid"] != first["pid"]
    assert adapter.process.poll() is not None
    assert host.managed_tool_session.current_adapter.policy["allowed_destinations"] == route


def test_resident_command_projection_retains_ordinary_child_network_denial(resident_host, monkeypatch, tmp_path):
    from astrid.core.execution.resident_sessions import ResidentPackAdapter

    root, host, _runtime = resident_host
    manifest = json.loads((root / "pack.yaml").read_text())
    manifest["actions"]["run"]["metadata"]["network_policy"] = {
        "allowed_protocols": ["tcp"], "allowed_destinations": ["127.0.0.1:1"],
    }
    (root / "pack.yaml").write_text(json.dumps(manifest))
    host.discover()
    record = host.capabilities["resident_probe.run"]
    adapter = ResidentPackAdapter.__new__(ResidentPackAdapter)
    adapter.dependency_pythonpath = ()
    monkeypatch.setattr(host.managed_tool_session, "adapter_for", lambda *_args, **_kwargs: adapter)
    attempt = tmp_path / "command-attempt"
    attempt.mkdir()
    probe = ("import socket,sys\n"
             "s=socket.socket()\n"
             "try:s.connect(('127.0.0.1',int(sys.argv[1])))\n"
             "except (PermissionError,RuntimeError):print('denied')\n"
             "else:raise SystemExit('unadmitted endpoint was accessible')\n")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()

        def dispatch(**kwargs):
            checked = subprocess.run([sys.executable, "-c", probe, str(listener.getsockname()[1])],
                env=kwargs["child_env"], cwd=attempt, capture_output=True, text=True, timeout=15)
            assert checked.returncode == 0 and checked.stdout.strip() == "denied", checked.stderr
            return {"checked": True}

        monkeypatch.setattr(host, "invoke_capability", dispatch)
        result = host._run_command_definition(record, {}, attempt / "outputs", attempt,
                                             resident_token=object(), allow_runtime_connection=True)
        assert result == {"checked": True}
