"""A rejected resident-host attach names the mismatching key on stderr and in its reason."""

import json
import os
from pathlib import Path

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


@pytest.fixture
def refreshed_registration(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from astrid.core.execution import generic_host
    from astrid_runtime_client.generated import Executor
    from tests.test_generic_host import FakeRuntime, _write_manifest

    current, ready, expected, worker = _attach_inputs(tmp_path)
    ready["python_executable"] = current["python_executable"]
    owner = worker.parent / "owner.token"
    owner.write_text("selected-runtime-owner", encoding="utf-8")
    host_bootstrap.platform.owner_only(owner)
    calls = []

    class Runtime(FakeRuntime):
        schema_digest = "schema-1"

        def health(self):
            return {**super().health(), "runtime_instance_id": "rt-1", "instance_id": "rt-1",
                    "coordinator_epoch": "rt-1"}

        def register_executor(self, executor_id, **payload):
            super().register_executor(executor_id, **payload)
            self.registration = Executor.from_json({**payload, "executor_id": executor_id,
                "protocol": payload["protocol_version"], "readiness": "ready"})
            return self.registration

    runtime = Runtime()
    packs = tmp_path / "packs"
    _write_manifest(packs / "echo")
    host = generic_host.GenericPackHost(pack_roots=[packs], client=runtime,
                                      executor_id=host_bootstrap.PACK_HOST_ACTOR)
    result = host.register()
    marker = Path(expected["ready_file"])
    host._ready_marker_path, host._ready_marker_context = marker, ready
    host._publish_ready_registration(result)
    before = json.loads(marker.read_text())
    _write_manifest(packs / "added", capability_id="test.added")
    host.refresh()
    host._renew_executor_registration(deliberate=True)
    assert runtime.registration.source_epoch != before["source_epoch"]

    class Client:
        def __init__(self, endpoint, token):
            assert endpoint == expected["endpoint"]
            assert token == "selected-runtime-owner"

        def health(self):
            return runtime.health()

        def _authenticate_worker(self, *_args):
            pytest.fail("owner observation must not authenticate as a worker")

        @property
        def generated(self):
            def observe(executor_id):
                calls.append(executor_id)
                return runtime.registration
            return SimpleNamespace(get_executor=observe)

    monkeypatch.setattr(generic_host, "RuntimeProtocolClient", Client)
    monkeypatch.setattr(generic_host, "source_checkout_digest", lambda _path: current["source_checkout_digest"])
    monkeypatch.setattr(generic_host, "source_checkout_closure_digest", lambda _path: current["source_closure_digest"])
    monkeypatch.setattr("astrid.core._shared.boot_manifest.load_boot_manifest_hash",
                        lambda *_args, **_kw: current["boot_manifest_hash"])
    return SimpleNamespace(current=current, ready=json.loads(marker.read_text()), expected=expected,
                           worker=worker, owner=owner, runtime=runtime, calls=calls,
                           host=host, marker=marker, packs=packs)


def _attach_refreshed(value, rejections):
    return host_bootstrap._attach_resident_host(
        value.current, value.ready, expected=value.expected, worker_path=value.worker,
        runtime_epoch=1, runtime_instance_id="rt-1", inventory_identity="", rejections=rejections,
    )


def test_attach_observes_current_structured_refresh_with_runtime_owner(refreshed_registration):
    value = refreshed_registration
    rejections = []
    receipt = _attach_refreshed(value, rejections)

    assert rejections == []
    assert receipt["host_pid"] == value.current["pid"]
    assert receipt["host_ready_capabilities"] == ["test.added", "test.echo"]
    assert value.calls == [host_bootstrap.PACK_HOST_ACTOR]
    registration = value.ready["registration"]["registration"]
    assert registration["source_epoch"] == value.runtime.registration.source_epoch
    assert registration["source_digest"] == value.runtime.registration.source_digest
    assert registration["dependency_digest"] == value.runtime.registration.dependency_digest


def test_failed_renewal_removes_ready_until_recovery(refreshed_registration, monkeypatch):
    from astrid.core.execution.generic_host import HostError
    from tests.test_generic_host import _write_manifest

    value = refreshed_registration
    previous = json.loads(value.marker.read_text())
    assert _attach_refreshed(value, []) is not None
    original_register = value.runtime.register_executor
    with monkeypatch.context() as patch:
        def fail(*_args, **_kwargs):
            raise HostError("registration lost or replaced")
        patch.setattr(value.runtime, "register_executor", fail)
        with pytest.raises(HostError, match="registration lost or replaced"):
            value.host._renew_executor_registration()

    assert value.host._registration_admission_reason()
    value.ready = host_bootstrap._read_object(value.marker) or {}
    assert _attach_refreshed(value, []) is None
    assert not value.marker.exists()

    _write_manifest(value.packs / "recovered", capability_id="test.recovered")
    value.host.refresh()
    assert value.runtime.register_executor == original_register
    value.host._renew_executor_registration(deliberate=True)
    value.ready = json.loads(value.marker.read_text())
    assert value.ready["status"] == "ready"
    assert value.ready["source_epoch"] != previous["source_epoch"]
    assert value.ready["registration"]["registration"]["source_digest"] != previous["registration"]["registration"]["source_digest"]
    assert value.ready["pid"] == value.current["pid"]
    assert value.ready["process_birth_id"] == value.current["process_birth_id"]
    assert value.ready["runtime_epoch"] == value.runtime.registration.runtime_epoch
    assert _attach_refreshed(value, []) is not None


@pytest.mark.parametrize("field", ["source_epoch", "source_digest", "dependency_digest"])
def test_attach_rejects_stale_nested_registration(refreshed_registration, field):
    value = refreshed_registration
    value.ready["registration"]["registration"][field] = "stale"
    rejections = []

    assert _attach_refreshed(value, rejections) is None
    assert any(field in reason for reason in rejections)


@pytest.mark.parametrize("fault", ["missing", "symlink", "permissions", "foreign-runtime"])
def test_attach_refuses_unsafe_owner_or_foreign_runtime(refreshed_registration, monkeypatch, fault):
    value = refreshed_registration
    if fault == "missing":
        value.owner.unlink()
    elif fault == "symlink":
        target = value.owner.with_name("other.token")
        value.owner.rename(target)
        value.owner.symlink_to(target)
    elif fault == "permissions":
        monkeypatch.setattr(host_bootstrap.platform, "is_owner_only", lambda _path: False)
    else:
        health = value.runtime.health()
        monkeypatch.setattr(value.runtime, "health", lambda: {**health, "runtime_instance_id": "foreign"})
    rejections = []

    assert _attach_refreshed(value, rejections) is None
    assert rejections
    assert value.calls == []
    assert "selected-runtime-owner" not in str(rejections)
@pytest.mark.integration
def test_serialized_runtime_registration_attaches_and_tampering_refuses(tmp_path, monkeypatch, real_runtime):
    """Use the real typed Runtime receipt and the resident host's own marker publisher.

    Attach observes the executor with the selected Runtime's owner credential, which
    sits beside the worker credential; the worker credential alone cannot observe it.
    """
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
    current, ready, expected, worker = _attach_inputs(tmp_path)
    worker.write_text(daemon.worker_token, encoding="utf-8")
    os.chmod(worker, 0o600)
    owner = worker.parent / "owner.token"
    owner.write_text(daemon.token, encoding="utf-8")
    os.chmod(owner, 0o600)
    epoch = host.runtime_state["runtime_epoch"]
    health = client.health()
    schema = health.get("schema_digest") if isinstance(health, dict) else getattr(health, "schema_digest")
    instance = health.get("runtime_instance_id") if isinstance(health, dict) else getattr(health, "runtime_instance_id")
    for obj in (current, ready, expected):
        obj.update(endpoint=daemon.endpoint, runtime_epoch=epoch, schema_digest=schema,
                   runtime_instance_id=instance)
    ready.update(python_executable=current["python_executable"])
    ready_file = tmp_path / "ready.json"
    host._ready_marker_path, host._ready_marker_context = ready_file, ready
    host._publish_ready_registration(result)
    ready = json.loads(ready_file.read_text())
    assert host_bootstrap._attach_static_mismatches(current, ready, expected, "") == []
    nested = ready["registration"]["registration"]
    assert isinstance(nested, dict)
    assert nested["source_epoch"] == ready["source_epoch"]
    monkeypatch.setattr(generic_host, "source_checkout_digest", lambda _: "digest-1")
    monkeypatch.setattr(generic_host, "source_checkout_closure_digest", lambda _: "closure-1")
    monkeypatch.setattr(boot_manifest, "load_boot_manifest_hash", lambda *a, **k: "hash-1")

    def attach(payload, rejections):
        return host_bootstrap._attach_resident_host(
            current, payload, expected=expected, worker_path=worker, runtime_epoch=epoch,
            runtime_instance_id=instance, inventory_identity="", rejections=rejections,
        )

    try:
        rejections = []
        assert attach(ready, rejections) is not None, rejections
        for key in ("source_epoch", "source_digest", "dependency_digest"):
            changed = json.loads(json.dumps(ready))
            changed["registration"]["registration"][key] = "0" * 64
            rejections = []
            assert attach(changed, rejections) is None
            assert rejections
        legacy = json.loads(json.dumps(ready))
        for key in ("source_epoch", "source_digest", "dependency_digest"):
            legacy["registration"]["registration"].pop(key)
        assert attach(legacy, []) is None
        worker_client = generic_host.RuntimeProtocolClient(daemon.endpoint, daemon.worker_token)
        worker_client._authenticate_worker(host_bootstrap.PACK_HOST_ACTOR)
        with pytest.raises(Exception):
            worker_client.generated.get_executor(host_bootstrap.PACK_HOST_ACTOR)
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
