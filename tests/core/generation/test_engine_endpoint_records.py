from dataclasses import FrozenInstanceError, fields
from typing import get_args

import pytest

from astrid.core.generation.engine_contracts import (
    EndpointAuth, EndpointOptions, EndpointProfileEvidence, EndpointState,
    EngineEndpoint, HealthEvidence, LifecycleOwner, LocalAttestation,
    RunPodAttestation, SecretRef,
)


def test_contract_fields_and_states():
    assert [f.name for f in fields(EngineEndpoint)] == [
        "schema", "endpoint_id", "engine", "platform", "placement", "url",
        "auth", "state", "health", "lifecycle_owner", "attestation",
    ]
    assert [f.name for f in fields(EndpointProfileEvidence)] == [
        "schema", "engine", "platform", "claim_operation_id", "claim_attestation",
    ]
    assert get_args(EndpointState) == (
        "planned", "acquiring", "starting", "ready", "failed", "stopping",
        "releasing", "released", "cleanup_pending",
    )
    assert EndpointOptions("linux-x86_64-cu130", "/engine", None).release_on_finish is True


def test_records_are_inert_and_frozen():
    health = HealthEvidence("healthy", "2026-10-27T00:00:00Z", "a" * 64, True)
    local = LocalAttestation("local-engine.v1", "/receipt", "b" * 64, health, "a" * 64)
    remote = RunPodAttestation("runpod-engine.v1", "account", "pod123", "/snapshot", "c" * 64, "/receipt", "b" * 64, health, "a" * 64)
    endpoint = EngineEndpoint("astrid.engine-endpoint.v1", "endpoint", "vibecomfy-h3", "linux-x86_64-cu130", "local", "http://127.0.0.1:8188", EndpointAuth("none", None), "ready", health, LifecycleOwner("attached", "owner", None, None, None, False), local)
    assert remote.claim_handle_path == "/snapshot"
    assert SecretRef("file", "/private/token").kind == "file"
    assert not hasattr(endpoint, "ensure") and not hasattr(endpoint, "release")
    with pytest.raises(FrozenInstanceError):
        endpoint.state = "released"

import builtins
import copy
import json
import os
import socket
from dataclasses import asdict, replace

from astrid.core.generation.engine_contracts import AttestationError
from astrid.core.generation.engine_endpoint import (
    endpoint_profile_evidence_to_dict, endpoint_to_dict, load_endpoint,
    load_endpoint_profile_evidence, parse_endpoint, parse_endpoint_profile_evidence,
)


@pytest.fixture
def records():
    health = HealthEvidence("healthy", "2026-10-27T00:00:00Z", "a" * 64, True)
    local = LocalAttestation("local-engine.v1", "/engine/install.json", "b" * 64, health, "a" * 64)
    remote = RunPodAttestation("runpod-engine.v1", "account", "pod123", "/private/snapshot", "c" * 64, "/engine/install.json", "b" * 64, health, "a" * 64)
    endpoint = EngineEndpoint("astrid.engine-endpoint.v1", "endpoint", "vibecomfy-h3", "linux-x86_64-cu130", "local", "http://127.0.0.1:8188", EndpointAuth("none", None), "ready", health, LifecycleOwner("attached", "owner", None, None, None, False), local)
    runpod = replace(endpoint, placement="runpod", url="https://pod123-8000.proxy.runpod.net/engines/endpoint", auth=EndpointAuth("bearer-file", SecretRef("file", "/private/token")), lifecycle_owner=LifecycleOwner("astrid-runpod", "owner", "/private/live", "job", "pod123", True), attestation=remote)
    profile = EndpointProfileEvidence("astrid.runpod.endpoint-evidence.v1", "vibecomfy-h3", "linux-x86_64-cu130", "operation", remote)
    return endpoint, runpod, profile


def test_roundtrips_are_pure(records, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected I/O")
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    from astrid.core.generation import engine_recipe
    monkeypatch.setattr(engine_recipe, "load_recipe", forbidden)
    monkeypatch.setattr(engine_recipe, "load_recipe_index", forbidden)
    for endpoint in records[:2]:
        wire = endpoint_to_dict(endpoint)
        assert parse_endpoint(wire) == endpoint
        assert load_endpoint(json.dumps(wire).encode()) == endpoint
        assert not hasattr(endpoint, "ensure") and not hasattr(endpoint, "release")
    profile = records[2]
    wire = endpoint_profile_evidence_to_dict(profile)
    assert parse_endpoint_profile_evidence(wire) == profile
    assert load_endpoint_profile_evidence(json.dumps(wire)) == profile
    assert wire["claim_attestation"]["claim_handle_path"] != records[1].lifecycle_owner.handle_path


@pytest.mark.parametrize("state", get_args(EndpointState))
def test_all_states_and_optional_evidence(records, state):
    value = asdict(records[1])
    value["state"] = state
    if state != "ready":
        value["health"] = value["attestation"] = None
        value["auth"] = {"kind": "none", "token": None}
    assert parse_endpoint(value).state == state


@pytest.mark.parametrize("path,value", [
    (("schema",), ""), (("endpoint_id",), "Bad"), (("engine",), "a_b"),
    (("platform",), "linux-unknown"), (("placement",), "remote"), (("state",), "unknown"),
    (("health", "status"), "bad"), (("health", "checked_at"), "2026-02-30T00:00:00Z"),
    (("health", "checked_at"), "2026-10-27T00:00:00+01:00"),
    (("health", "required_nodes_present"), 1), (("health", "object_info_sha256"), "A" * 64),
    (("attestation", "install_receipt_sha256"), "sha256:" + "a" * 64),
    (("attestation", "object_info_sha256"), "d" * 64),
    (("attestation", "install_receipt_path"), "/a/../b"),
    (("attestation", "claim_handle_path"), "/a//b"),
    (("auth", "token", "path"), "relative"), (("auth", "token", "kind"), "raw"),
    (("lifecycle_owner", "release_on_finish"), 1), (("lifecycle_owner", "pod_id"), "other"),
    (("attestation", "pod_id"), "other"), (("platform",), "windows-x86_64-cu130"),
])
def test_invalid_nested_fields(records, path, value):
    wire = asdict(records[1])
    target = wire
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(AttestationError):
        parse_endpoint(wire)


@pytest.mark.parametrize("url", [
    "https://pod123-8000.proxy.runpod.net/engines/other",
    "https://pod123-8000.proxy.runpod.net/engines/endpoint/",
    "https://pod123-8000.proxy.runpod.net/engines/%65ndpoint",
    "https://pod123-8000.proxy.runpod.net/engines/endpoint?",
    "https://pod123-8000.proxy.runpod.net/engines/endpoint#x",
    "https://user@pod123-8000.proxy.runpod.net/engines/endpoint",
    "https://pod123-8000.proxy.runpod.net:443/engines/endpoint",
    "https://other-8000.proxy.runpod.net/engines/endpoint",
    "https://pod123-0.proxy.runpod.net/engines/endpoint",
    "https://pod123-65536.proxy.runpod.net/engines/endpoint",
    "https://pod123-08000.proxy.runpod.net/engines/endpoint",
])
def test_remote_url_exact(records, url):
    wire = asdict(records[1]); wire["url"] = url
    with pytest.raises(AttestationError): parse_endpoint(wire)


@pytest.mark.parametrize("url", ["http://localhost:8188", "http://127.0.0.1:8188/", "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://u@127.0.0.1:8188", "http://127.0.0.1:8188?", "https://127.0.0.1:8188"])
def test_local_url_exact(records, url):
    wire = asdict(records[0]); wire["url"] = url
    with pytest.raises(AttestationError): parse_endpoint(wire)


def test_closed_shapes_duplicate_json_and_type_refusal(records):
    for original, parse, load in [(records[1], parse_endpoint, load_endpoint), (records[2], parse_endpoint_profile_evidence, load_endpoint_profile_evidence)]:
        wire = asdict(original)
        for changed in ({**wire, "extra": 1}, {k: v for k, v in wire.items() if k != "schema"}, {}, []):
            with pytest.raises(AttestationError): parse(changed)
        text = json.dumps(wire)
        with pytest.raises(AttestationError): load(text[:-1] + ',"schema":"duplicate"}')
        with pytest.raises(AttestationError): load(text.replace('"kind": "runpod-engine.v1"', '"kind": "runpod-engine.v1", "kind": "runpod-engine.v1"'))
        with pytest.raises(AttestationError): load(b'\xff')
        with pytest.raises(AttestationError): load('{"x":NaN}')


def test_ready_and_ownership_fences(records):
    wire = asdict(records[1])
    for mutation in (
        lambda x: x.update(health=None), lambda x: x.update(attestation=None),
        lambda x: x.update(auth={"kind": "none", "token": None}),
        lambda x: x["health"].update(status="unhealthy"),
        lambda x: x["lifecycle_owner"].update(kind="attached", release_on_finish=True),
        lambda x: x["attestation"].update(kind="local-engine.v1"),
    ):
        changed = copy.deepcopy(wire); mutation(changed)
        with pytest.raises(AttestationError): parse_endpoint(changed)
    wire = asdict(records[0]); wire["attestation"] = asdict(records[1].attestation)
    with pytest.raises(AttestationError): parse_endpoint(wire)


def test_profile_strict_and_unknown_engine_is_inert(records):
    wire = asdict(records[2]); wire["engine"] = "unlisted-engine"
    assert parse_endpoint_profile_evidence(wire).engine == "unlisted-engine"
    endpoint = asdict(records[1]); endpoint["engine"] = "unlisted-engine"
    assert parse_endpoint(endpoint).engine == "unlisted-engine"
    wire["platform"] = "windows-x86_64-cu130"
    with pytest.raises(AttestationError): parse_endpoint_profile_evidence(wire)
    wire = asdict(records[2]); wire["claim_attestation"] = asdict(records[0].attestation)
    with pytest.raises(AttestationError): parse_endpoint_profile_evidence(wire)


def test_serializers_reject_untyped_nested_records(records):
    endpoint = replace(records[1], auth=asdict(records[1].auth))
    with pytest.raises(AttestationError):
        endpoint_to_dict(endpoint)
    profile = replace(records[2], claim_attestation=asdict(records[2].claim_attestation))
    with pytest.raises(AttestationError):
        endpoint_profile_evidence_to_dict(profile)
