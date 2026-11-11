"""A rejected resident-host attach names the mismatching key on stderr and in its reason."""

import json
import os

import pytest

from astrid.sdk import host_bootstrap


def _attach_inputs(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    worker = tmp_path / "credentials" / "astrid-pack-host.token"
    worker.parent.mkdir()
    worker.write_text("not-a-real-token", encoding="utf-8")
    venv_python = str(tmp_path / "venv" / "Scripts" / "python.exe")
    shared = {
        "endpoint": "http://127.0.0.1:50931",
        "executor_id": host_bootstrap.PACK_HOST_ACTOR,
        "ready_file": str(tmp_path / "generic-host.ready.json"),
        "credential_file": str(worker),
        "support_root": str(tmp_path),
        "runtime_instance_id": "rt-1",
        "runtime_epoch": 1,
        "schema_digest": "schema-1",
        "source_checkout": str(checkout),
        "source_checkout_digest": "digest-1",
        "source_closure_digest": "closure-1",
        "source_inventory_identity": "",
        "boot_manifest_path": str(tmp_path / "boot-manifest.json"),
        "boot_manifest_hash": "hash-1",
        "python_executable": venv_python,
    }
    current = {**shared, "pid": 4321, "process_birth_id": "windows-creation:1234",
               "runtime_service_launch_id": "a" * 32}
    ready = {**shared, "status": "ready", "pid": 4321, "process_birth_id": "windows-creation:1234",
             "python_executable": str(tmp_path / "base" / "python.exe")}
    expected = dict(shared)
    return current, ready, expected, worker


def test_rejected_attach_names_mismatched_ready_key_on_stderr(tmp_path, capsys):
    current, ready, expected, worker = _attach_inputs(tmp_path)
    rejections = []

    receipt = host_bootstrap._attach_resident_host(
        current, ready, expected=expected, worker_path=worker, runtime_epoch=1,
        runtime_instance_id="rt-1", inventory_identity="", rejections=rejections,
    )

    assert receipt is None
    assert any("ready.python_executable" in reason for reason in rejections)
    diagnostics = [json.loads(line) for line in capsys.readouterr().err.splitlines()
                   if line.startswith('{"diagnostic"')]
    rejected = [item for item in diagnostics if item["diagnostic"] == "astrid.pack_host_attach_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["pid"] == 4321
    assert rejected[0]["runtime_service_launch_id"] == "a" * 32
    assert any(key == "ready.python_executable" for key in rejected[0]["keys"])
    assert "token" not in json.dumps(rejected[0]).lower()


def test_refusal_text_carries_the_attach_reason(monkeypatch):
    monkeypatch.setattr(host_bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "ours")
    reason = "attach rejected: ready.python_executable expected='venv' actual='base'"

    try:
        host_bootstrap._terminate_old_host(
            {"pid": 4321, "process_birth_id": "windows-creation:1234"}, attach_reason=reason)
    except host_bootstrap.PackHostBootstrapError as error:
        message = str(error)
    else:
        raise AssertionError("live Windows host without custody must be refused")
    assert message.startswith("registered Windows host has no retained Job custody; retirement blocked")
    assert "(registration status ours; " in message
    assert message.endswith(f"; {reason})")


@pytest.mark.integration
def test_serialized_runtime_registration_attaches_and_tampering_refuses(tmp_path, monkeypatch, real_runtime):
    """Use the real typed Runtime receipt and the same serializer as a resident host."""
    from astrid.core.execution import generic_host
    from astrid.core._shared import boot_manifest
    from tests.test_generic_host import _write_manifest

    daemon = real_runtime()
    pack = tmp_path / "pack"
    _write_manifest(pack)
    client = generic_host.RuntimeProtocolClient(daemon.endpoint, daemon.worker_token)
    host = generic_host.GenericPackHost(pack_roots=[tmp_path], client=client)
    host.discover()
    result = host.register()
    envelope = generic_host._ready_registration_envelope(result)
    current, ready, expected, worker = _attach_inputs(tmp_path)
    worker.write_text(daemon.worker_token, encoding="utf-8")
    os.chmod(worker, 0o600)
    epoch = host.runtime_state["runtime_epoch"]
    for obj in (current, ready, expected):
        obj.update(endpoint=daemon.endpoint, runtime_epoch=epoch)
    ready.update(source_epoch=host.source_epoch, registration=envelope)
    ready_file = tmp_path / "ready.json"
    generic_host._write_ready_marker(ready_file, ready)
    ready = json.loads(ready_file.read_text())
    assert isinstance(ready["registration"]["registration"], str)
    assert ready["registration"]["source_epoch"] == ready["source_epoch"]
    monkeypatch.setattr(generic_host, "source_checkout_digest", lambda _: "digest-1")
    monkeypatch.setattr(generic_host, "source_checkout_closure_digest", lambda _: "closure-1")
    monkeypatch.setattr(boot_manifest, "load_boot_manifest_hash", lambda *a, **k: "hash-1")

    def attach(payload):
        return host_bootstrap._attach_resident_host(
            current, payload, expected=expected, worker_path=worker, runtime_epoch=epoch,
            runtime_instance_id="instance-1", inventory_identity="",
        )

    try:
        assert attach(ready) is not None
        for key in ("source_epoch", "source_digest", "dependency_digest"):
            changed = json.loads(json.dumps(ready))
            changed["registration"][key] = "0" * 64
            assert attach(changed) is None
        legacy = json.loads(json.dumps(ready))
        for key in ("source_epoch", "source_digest", "dependency_digest"):
            legacy["registration"].pop(key)
        assert attach(legacy) is None
    finally:
        host.shutdown()


@pytest.mark.parametrize("field", ["source_epoch", "source_digest", "dependency_digest"])
def test_registration_response_must_match_submitted_identity(field):
    from types import SimpleNamespace
    from astrid.core.execution.generic_host import HostError, _executor_registration_identity

    expected = {key: "a" * 64 for key in ("source_epoch", "source_digest", "dependency_digest")}
    accepted = dict(expected)
    accepted[field] = "b" * 64
    with pytest.raises(HostError, match=field):
        _executor_registration_identity(SimpleNamespace(**accepted), expected)
    accepted[field] = None
    with pytest.raises(HostError, match=field):
        _executor_registration_identity(SimpleNamespace(**accepted), expected)
