"""CPU reproduction at install and engine-start custody boundaries."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

from tests.core.generation.test_engine_custody import cpu_h3_install as cpu_installed, session  # noqa: F401
from astrid.core.generation.engine_runner import _json, plan_install

PLATFORM = "linux-x86_64-cu130"


def _legacy_owner(root: Path, identity: dict, *, helper_sha: str) -> dict:
    old = {**identity, "helper_sha256": helper_sha}
    control = root / ".astrid-engine" / "session"
    control.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root / ".astrid-engine", 0o700)
    os.chmod(control, 0o700)
    (control / "owner.lock").touch(mode=0o600)
    (control / "session.json").write_text(_json({
        "identity": old, "owner_id": "b" * 32,
    }))
    os.chmod(control / "owner.lock", 0o600)
    os.chmod(control / "session.json", 0o600)
    return old


def test_install_engine_converges_after_dead_legacy_owner(cpu_installed):
    """Public installer converges the real CPU installed receipt after owner death."""
    from astrid.core.generation import engine_runner
    from astrid.core.generation.engine_execution import create_local_backend

    client, _payload, recipe, root, _started, _program_path = cpu_installed
    client.stop_owned(deadline=time.monotonic() + 15)
    current = client.identity
    _legacy_owner(root, current, helper_sha="a" * 64)
    deadline = time.monotonic() + 60
    context, adapter = create_local_backend(engine=recipe.engine, platform=recipe.platform,
                                            placement="local", root=str(root), deadline=deadline)
    plan = plan_install(recipe, root=str(root), placement="local")
    receipt = engine_runner.install_engine(plan, context=context, adapter=adapter, deadline=deadline)
    assert receipt.status == "installed"
    marker = json.loads((root / ".astrid-engine/session/session.json").read_text())
    assert marker["identity"] == current
    assert isinstance(marker.get("process", {}).get("pid"), int)
    assert isinstance(marker["process"].get("birth_id"), str)
    assert next(step for step in receipt.steps if step.kind == "health").validation == "passed"


def test_start_replaces_dead_legacy_owner_with_real_installed_receipt(cpu_installed):
    """Start separately crosses the real installed receipt and service boundary."""
    client, payload, recipe, root, _started, _program_path = cpu_installed
    # Stop the actual fixture owner and its tiny HTTP engine before publishing
    # the old marker, leaving the native owner lock available.
    client.stop_owned(deadline=time.monotonic() + 15)

    legacy = _legacy_owner(root, client.identity, helper_sha="a" * 64)
    marker = json.loads((root / ".astrid-engine/session/session.json").read_text())
    assert set(marker) == {"identity", "owner_id"}
    assert marker["identity"] == legacy

    result = client.call("start", payload, deadline=time.monotonic() + 12)
    assert result["state"] == "owned"
    receipt = json.loads((root / ".astrid-engine/install.json").read_text())
    assert receipt["status"] == "installed" and "helper_sha256" not in receipt
    marker = json.loads((root / ".astrid-engine/session/session.json").read_text())
    assert marker["process"]["pid"] == client._spawned.pid
    assert isinstance(marker["process"]["birth_id"], str)
