"""Fail-closed checks for the explicit Astrid runtime handshake boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from astrid.sdk import autobootstrap
from astrid.sdk.client import AstridClient
from astrid.sdk.exceptions import ServiceUnavailableError
from astrid.sdk.workspace_client import (
    PROTOCOL,
    SCHEMA_DIGEST,
    WorkspaceClientError,
    resolve_runtime_connection,
)

TARGETED_EXECUTION_BINDING_CAPABILITY = "execution_binding.targeted.v1"


class _Workspace:
    def __init__(self, health: object, handshake: object) -> None:
        self._health = health
        self._handshake = handshake

    def health(self) -> object:
        return self._health

    def handshake(self, *_args: object) -> object:
        return self._handshake


def _health(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "status": "ok",
        "protocol": PROTOCOL,
        "schema_digest": SCHEMA_DIGEST,
        "runtime_epoch": 1,
        "runtime_instance_id": "instance-1",
        "runtime_session_id": "runtime-session-1",
        "identity": {"release": 1, "managed_by": "pinokio"},
    }
    value.update(overrides)
    return value


def _health_without(field: str) -> dict[str, object]:
    value = _health()
    del value[field]
    return value


def _handshake(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "protocol": PROTOCOL,
        "schema_digest": SCHEMA_DIGEST,
        "session_id": "session-1",
        "actor_id": "actor-1",
        "realm_id": "realm-1",
        "scopes": [
            "projects:read",
            "projects:write",
            "objects:read",
            "objects:write",
            "tasks:read",
            "tasks:write",
        ],
        "capabilities": [TARGETED_EXECUTION_BINDING_CAPABILITY],
    }
    value.update(overrides)
    return value


def _handshake_without(field: str) -> dict[str, object]:
    value = _handshake()
    del value[field]
    return value


@pytest.mark.parametrize(
    "response",
    [
        _health(protocol="workspace.v999"),
        _health(schema_digest="sha256:" + "0" * 64),
        _health(status="degraded"),
        _health(runtime_instance_id=""),
        _health(runtime_session_id=42),
        _health_without("runtime_instance_id"),
        _health_without("runtime_session_id"),
        _health_without("identity"),
        _health(extra="tampered"),
    ],
)
def test_open_rejects_tampered_health(monkeypatch: pytest.MonkeyPatch, response: object) -> None:
    monkeypatch.setattr(
        "astrid.sdk.workspace_client.WorkspaceClient",
        lambda *_args: _Workspace(response, _handshake()),
    )
    with pytest.raises(ServiceUnavailableError) as error:
        AstridClient.open(
            endpoint="http://127.0.0.1:1",
            credential="token",
            realm_id="realm-1",
            actor_id="actor-1",
            client_name="test",
            client_version="1",
            protocol_version=PROTOCOL,
        )
    assert error.value.details["reason"] in {"protocol_error", "identity_mismatch"}
    assert error.value.details["next_action"] == "run the Astrid install line again"


def _service(**overrides: object) -> dict[str, object]:
    return {"name": "chat", "state": "running", "last_exit": None, "restart_count": 0, **overrides}


def _open_with_health(monkeypatch: pytest.MonkeyPatch, response: object) -> AstridClient:
    monkeypatch.setattr(
        "astrid.sdk.workspace_client.WorkspaceClient",
        lambda *_args: _Workspace(response, _handshake()),
    )
    return AstridClient.open(
        endpoint="http://127.0.0.1:1", credential="token", realm_id="realm-1", actor_id="actor-1",
        client_name="test", client_version="1", protocol_version=PROTOCOL,
    )


def _source_identity(**overrides: object) -> dict[str, object]:
    return {
        "release": 0,
        "managed_by": "source",
        "runtime_head": "a" * 40,
        "agent_head": "b" * 40,
        "dirty": False,
        "dirty_hash": "c" * 64,
        **overrides,
    }


@pytest.mark.parametrize("identity", [
    None,
    {"release": 0, "managed_by": None},
    {"release": 1, "managed_by": None},
    {"release": 0, "managed_by": "pinokio"},
    {"release": 17, "managed_by": "pinokio"},
    _source_identity(),
    _source_identity(runtime_head="a" * 64, agent_head="b" * 64, dirty=True),
])
def test_open_accepts_declared_health_identity(monkeypatch: pytest.MonkeyPatch, identity: object) -> None:
    assert isinstance(_open_with_health(monkeypatch, _health(identity=identity)), AstridClient)


@pytest.mark.parametrize("identity,field", [
    ([], "health.identity"),
    ("source", "health.identity"),
    (False, "health.identity"),
    (0, "health.identity"),
    ({}, "health.identity"),
    ({"release": 1}, "health.identity"),
    ({"managed_by": "pinokio"}, "health.identity"),
    ({"release": 1, "managed_by": "pinokio", "extra": "tampered"}, "health.identity"),
    ({"release": 0, "managed_by": "source"}, "health.identity"),
    ({"release": True, "managed_by": "pinokio"}, "health.identity.release"),
    ({"release": -1, "managed_by": "pinokio"}, "health.identity.release"),
    ({"release": 1.0, "managed_by": "pinokio"}, "health.identity.release"),
    ({"release": "1", "managed_by": "pinokio"}, "health.identity.release"),
    ({"release": None, "managed_by": "pinokio"}, "health.identity.release"),
    ({"release": 1, "managed_by": "other"}, "health.identity.managed_by"),
    ({"release": 1, "managed_by": False}, "health.identity.managed_by"),
    ({"release": 1, "managed_by": []}, "health.identity.managed_by"),
    (_source_identity(extra="tampered"), "health.identity"),
    (_source_identity(managed_by=None), "health.identity"),
    (_source_identity(managed_by="pinokio"), "health.identity"),
    (_source_identity(release=1), "health.identity.release"),
    (_source_identity(release=False), "health.identity.release"),
    (_source_identity(release=0.0), "health.identity.release"),
    (_source_identity(dirty=0), "health.identity.dirty"),
    (_source_identity(dirty="false"), "health.identity.dirty"),
    (_source_identity(dirty=None), "health.identity.dirty"),
])
def test_open_rejects_malformed_health_identity(monkeypatch: pytest.MonkeyPatch, identity: object, field: str) -> None:
    with pytest.raises(ServiceUnavailableError) as error:
        _open_with_health(monkeypatch, _health(identity=identity))
    assert error.value.details["reason"] == "protocol_error"
    assert error.value.details["field"] == field


@pytest.mark.parametrize("field", ["release", "managed_by", "runtime_head", "agent_head", "dirty", "dirty_hash"])
def test_source_health_identity_requires_each_declared_field(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    identity = _source_identity()
    del identity[field]
    with pytest.raises(ServiceUnavailableError) as error:
        _open_with_health(monkeypatch, _health(identity=identity))
    assert error.value.details["reason"] == "protocol_error"
    assert error.value.details["field"] == "health.identity"


@pytest.mark.parametrize("field", ["runtime_head", "agent_head"])
@pytest.mark.parametrize("head", [None, 0, "a" * 39, "a" * 41, "a" * 63, "a" * 65, "A" * 40, "g" * 64])
def test_source_health_identity_rejects_malformed_heads(monkeypatch: pytest.MonkeyPatch, field: str, head: object) -> None:
    with pytest.raises(ServiceUnavailableError) as error:
        _open_with_health(monkeypatch, _health(identity=_source_identity(**{field: head})))
    assert error.value.details["reason"] == "protocol_error"
    assert error.value.details["field"] == f"health.identity.{field}"


@pytest.mark.parametrize("dirty_hash", [None, 0, "c" * 40, "c" * 63, "c" * 65, "C" * 64, "g" * 64])
def test_source_health_identity_rejects_malformed_dirty_hash(monkeypatch: pytest.MonkeyPatch, dirty_hash: object) -> None:
    with pytest.raises(ServiceUnavailableError) as error:
        _open_with_health(monkeypatch, _health(identity=_source_identity(dirty_hash=dirty_hash)))
    assert error.value.details["reason"] == "protocol_error"
    assert error.value.details["field"] == "health.identity.dirty_hash"


@pytest.mark.parametrize("services", [
    [],
    [_service(state=state) for state in ("starting", "running", "restarting", "failed")],
    [_service(last_exit=0, restart_count=1, reason=None)],
    [_service(last_exit=-9, restart_count=2, reason="restarting after signal")],
    [_service(last_exit=1, reason="")],
])
def test_open_accepts_declared_optional_service_health(monkeypatch: pytest.MonkeyPatch, services: object) -> None:
    assert isinstance(_open_with_health(monkeypatch, _health(services=services)), AstridClient)


@pytest.mark.parametrize("services,field", [
    (None, "health.services"),
    ({}, "health.services"),
    ("running", "health.services"),
    ((), "health.services"),
    ([None], "health.services[0]"),
    ([[]], "health.services[0]"),
    ([_service(extra="tampered")], "health.services[0]"),
    ([_service(name="")], "health.services[0].name"),
    ([_service(name=1)], "health.services[0].name"),
    ([_service(state="stopped")], "health.services[0].state"),
    ([_service(state={})], "health.services[0].state"),
    ([_service(last_exit=False)], "health.services[0].last_exit"),
    ([_service(last_exit=1.5)], "health.services[0].last_exit"),
    ([_service(last_exit="0")], "health.services[0].last_exit"),
    ([_service(restart_count=False)], "health.services[0].restart_count"),
    ([_service(restart_count=-1)], "health.services[0].restart_count"),
    ([_service(restart_count=1.5)], "health.services[0].restart_count"),
    ([_service(restart_count="0")], "health.services[0].restart_count"),
    ([_service(reason=False)], "health.services[0].reason"),
    ([_service(reason=[])], "health.services[0].reason"),
])
def test_open_rejects_malformed_service_health(monkeypatch: pytest.MonkeyPatch, services: object, field: str) -> None:
    with pytest.raises(ServiceUnavailableError) as error:
        _open_with_health(monkeypatch, _health(services=services))
    assert error.value.details["reason"] == "protocol_error"
    assert error.value.details["field"] == field


@pytest.mark.parametrize("field", ["name", "state", "last_exit", "restart_count"])
def test_service_health_requires_each_declared_row_field(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    service = _service()
    del service[field]
    with pytest.raises(ServiceUnavailableError) as error:
        _open_with_health(monkeypatch, _health(services=[service]))
    assert error.value.details["field"] == "health.services[0]"


@pytest.mark.parametrize("field", ["status", "protocol", "schema_digest", "runtime_epoch",
                                 "runtime_instance_id", "runtime_session_id", "identity"])
def test_service_health_keeps_all_top_level_fields_required(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    health = _health(services=[])
    del health[field]
    with pytest.raises(ServiceUnavailableError) as error:
        _open_with_health(monkeypatch, health)
    assert error.value.details["field"] == "health"


@pytest.mark.parametrize("response,field", [
    (_health(services=[], extra="tampered"), "health"),
    (_health(services=[], protocol="workspace.v999"), "health.protocol"),
    (_health(services=[], schema_digest="sha256:" + "0" * 64), "health.schema_digest"),
])
def test_optional_services_do_not_relax_health_identity_checks(monkeypatch: pytest.MonkeyPatch, response: object, field: str) -> None:
    with pytest.raises(ServiceUnavailableError) as error:
        _open_with_health(monkeypatch, response)
    assert error.value.details["reason"] == "protocol_error"
    assert error.value.details["field"] == field


@pytest.mark.parametrize(
    "response",
    [
        _handshake(protocol="workspace.v999"),
        _handshake(schema_digest="sha256:" + "0" * 64),
        _handshake(session_id=""),
        _handshake(actor_id="attacker"),
        _handshake(realm_id="wrong-realm"),
        _handshake(scopes=["projects:read"]),
        _handshake(scopes=[
            "projects:read", "projects:write", "objects:read", "objects:write",
            "tasks:read", "tasks:write", "admin",
        ]),
        _handshake(extra="tampered"),
        _handshake_without("capabilities"),
        _handshake(capabilities="execution_binding.targeted.v1"),
        _handshake(capabilities=[]),
        _handshake(capabilities=[""]),
        _handshake(capabilities=[42]),
        _handshake(capabilities=["other.v1"]),
    ],
)
def test_open_rejects_tampered_handshake(monkeypatch: pytest.MonkeyPatch, response: object) -> None:
    monkeypatch.setattr(
        "astrid.sdk.workspace_client.WorkspaceClient",
        lambda *_args: _Workspace(_health(), response),
    )
    with pytest.raises(ServiceUnavailableError) as error:
        AstridClient.open(
            endpoint="http://127.0.0.1:1",
            credential="token",
            realm_id="realm-1",
            actor_id="actor-1",
            client_name="test",
            client_version="1",
            protocol_version=PROTOCOL,
        )
    assert error.value.details["reason"] in {"protocol_error", "identity_mismatch"}


def test_connection_rejects_non_loopback_and_symlinked_credentials(
    tmp_path: Path,
) -> None:
    credential = tmp_path / "credential.json"
    credential.write_text('{"token":"secret"}', encoding="utf-8")
    symlink = tmp_path / "credential-link.json"
    symlink.symlink_to(credential)
    with pytest.raises(WorkspaceClientError, match="loopback"):
        resolve_runtime_connection("https://runtime.example", credential)
    with pytest.raises(WorkspaceClientError, match="symlink"):
        resolve_runtime_connection("http://127.0.0.1:1", symlink)


def test_launcher_rejects_symlinked_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_manifest = tmp_path / "real-manifest.json"
    real_manifest.write_text('{"profile":"astrid"}', encoding="utf-8")
    link = tmp_path / "manifest-link.json"
    link.symlink_to(real_manifest)
    monkeypatch.setenv("ASTRID_RUNTIME_SOURCE_MANIFEST", str(link))
    with pytest.raises(autobootstrap.AutoBootstrapError, match="source manifest is unsafe"):
        autobootstrap.ensure_runtime()


def test_launcher_rejects_non_loopback_advertisement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"profile":"astrid"}', encoding="utf-8")
    monkeypatch.setenv("ASTRID_RUNTIME_SOURCE_MANIFEST", str(manifest))
    monkeypatch.setenv("ASTRID_RUNTIME_LAUNCHER", "/bin/true")
    monkeypatch.setattr(
        autobootstrap.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            '{"status":"started","realm_id":"realm","actor_id":"actor","endpoint":"https://runtime.example"}',
            "",
        ),
    )
    with pytest.raises(autobootstrap.AutoBootstrapError, match="unsafe endpoint"):
        autobootstrap.ensure_runtime()


def test_launcher_credential_environment_is_read_as_a_safe_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    credential = tmp_path / "owner.token"
    credential.write_text("secret-token\n", encoding="utf-8")
    monkeypatch.setenv("ASTRID_RUNTIME_CREDENTIAL", str(credential))
    monkeypatch.setattr(
        autobootstrap,
        "ensure_runtime",
        lambda: {
            "status": "reconnected",
            "realm_id": "realm-1",
            "endpoint": "http://127.0.0.1:1",
            "actor_id": "actor-1",
            "credential_file": "",
        },
    )
    def fake_workspace(endpoint: str, token: str) -> _Workspace:
        assert endpoint == "http://127.0.0.1:1"
        assert token == "secret-token"
        return _Workspace(_health(), _handshake())

    monkeypatch.setattr("astrid.sdk.workspace_client.WorkspaceClient", fake_workspace)
    AstridClient.open_from_launcher(
        client_name="test", client_version="1", protocol_version=PROTOCOL
    )
