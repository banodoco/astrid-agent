"""Delegated leaf commands enter strict network startup without a child channel."""
from types import SimpleNamespace

import pytest
import yaml

from astrid.core.execution.executor.folder import load_folder_executors
from astrid.core.execution.generic_host import CapabilityRecord, GenericPackHost, HostError


def _command(tmp_path, form):
    root = tmp_path / "leaf"
    root.mkdir()
    marker = tmp_path / "command-entered"
    code = (
        "import os, socket\n"
        "from pathlib import Path\n"
        "from astrid.sdk import _child_bridge\n"
        "from astrid.core.execution.network_policy import NetworkPolicyError\n"
        "assert _child_bridge._bridge is None\n"
        "assert 'ASTRID_RUNTIME_CREDENTIAL' not in os.environ\n"
        "assert 'ASTRID_RUNTIME_ENDPOINT' not in os.environ\n"
        "try:\n"
        "    socket.create_connection(('127.0.0.1', 9), timeout=0.1)\n"
        "except NetworkPolicyError:\n"
        f"    Path({str(marker)!r}).write_text('policy-installed')\n"
        "else:\n"
        "    raise AssertionError('network policy was bypassed')\n"
    )
    if form == "-m":
        (root / "command.py").write_text(code)
        args = ["-m", "command"]
    else:
        args = ["-c", code]
    (root / "pack.yaml").write_text(yaml.safe_dump({
        "schema_version": 3, "id": "leaf", "name": "Leaf", "version": "1.0.0",
        "actions": {"run": {
            "description": "Leaf network command", "inputs": [], "outputs": [],
            "isolation": {"mode": "subprocess", "network": True},
            "metadata": {"network_policy": {"allowed_protocols": ["dns", "tcp"], "allowed_routes": []}},
            "invocation": {"kind": "command", "command": {"argv": ["{python_exec}", *args]}},
        }},
    }))
    (definition,) = load_folder_executors(root)
    record = CapabilityRecord(definition, "fixture", "fixture", root)
    # Marker values prove private environment stripping; these are no account credentials.
    client = SimpleNamespace(endpoint="http://unused.invalid", credential="fixture-worker-marker")
    host = GenericPackHost(pack_roots=[root], client=client)
    attempt = tmp_path / "attempt"
    (attempt / "outputs").mkdir(parents=True)
    return host, record, attempt, marker


@pytest.mark.parametrize("form", ["-m", "-c"])
@pytest.mark.parametrize("corruption", [None, "missing_policy", "missing_hook", "changed_hook"])
def test_private_leaf_command_installs_network_before_pack_code(tmp_path, monkeypatch, form, corruption):
    host, record, attempt, marker = _command(tmp_path, form)
    environment = host._child_environment

    def child_environment(*args, **kwargs):
        env, secrets = environment(*args, **kwargs)
        hook = attempt / ".astrid-network-hook" / "sitecustomize.py"
        if corruption == "missing_policy":
            env.pop("ASTRID_NETWORK_POLICY")
        elif corruption == "missing_hook":
            hook.unlink()
        elif corruption == "changed_hook":
            hook.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        return env, secrets

    monkeypatch.setattr(host, "_child_environment", child_environment)
    if corruption:
        with pytest.raises(HostError, match="astrid network startup failed"):
            host._run_command_definition(record, {}, attempt / "outputs", attempt, allow_runtime_connection=False)
        assert not marker.exists()
    else:
        result = host._run_command_definition(record, {}, attempt / "outputs", attempt, allow_runtime_connection=False)
        assert result.returncode == 0 and marker.read_text() == "policy-installed"
    assert not host._active_processes


def test_private_network_command_rejects_other_interpreter(tmp_path):
    from dataclasses import replace

    host, record, attempt, marker = _command(tmp_path, "-m")
    record = replace(record, definition=replace(
        record.definition, command=replace(record.definition.command, argv=("/bin/sh", "-c", "exit 0")),
    ))
    with pytest.raises(HostError, match="admitted Python interpreter"):
        host._run_command_definition(record, {}, attempt / "outputs", attempt, allow_runtime_connection=False)
    assert not marker.exists() and not host._active_processes
