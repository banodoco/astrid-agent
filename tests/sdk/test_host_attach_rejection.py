"""A rejected resident-host attach names the mismatching key on stderr and in its reason."""

import json

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
    assert message.endswith(f"({reason})")
