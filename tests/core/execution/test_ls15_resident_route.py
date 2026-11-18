"""LS-15: reuse is bound to the effective, host-admitted network route."""
from __future__ import annotations

import json

from astrid.core.execution import generic_host
from tests.core.execution.test_resident_sessions import run_job
from tests.core.execution.test_resident_sessions import resident_host as resident_host


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
