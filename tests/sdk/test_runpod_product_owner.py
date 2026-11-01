"""Public-client owner, canonical custody and actual local engine HTTP/TLS.

No owner injection, provider credential resolution, GPU, or live RunPod calls.
The missing F07 publication is exercised as an explicit pre-mutation refusal.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation.engine_endpoint import endpoint_to_dict
from astrid.core.generation.preflight import GenerationReadinessError, runpod_generation_owner
from astrid.packs.runpod.target_selection import canonical_claim_path, save_runpod_preferences
from astrid.packs.runpod.worker_preparation import write_target_record
from astrid.sdk.client import AstridClient
from astrid.sdk.generation import GenerationFacade
from tests.core.generation.test_engine_endpoint_transport import (
    endpoint, engine_configuration, local_tls, network, observation_configuration, prepared,
)
from tests.packs.runpod.test_worker_preparation import _quiescence


@pytest.fixture
def product(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    preferences = {
        "provider_account_ref": "account", "gpu_types": ["gpu"],
        "image": "worker@sha256:" + "a" * 64,
        "volume": {"id": "volume", "name": "name", "size_gb": 200, "mount_path": "/workspace"},
    }
    save_runpod_preferences(preferences)
    client = AstridClient(remote=SimpleNamespace())
    owner = runpod_generation_owner(client)
    operation = "a" * 32
    claim = {
        "operation_id": operation, "request_name": "astrid-product-" + operation,
        "name": "astrid-product-" + operation, "state": "claimed", "pod_id": "pod123",
        "provider_account_ref": "account", "gpu_type": "gpu", "worker_image": preferences["image"],
        "network_volume_id": "volume", "network_volume_name": "name", "network_volume_size_gb": 200,
        "volume_mount_path": "/workspace", "acquisition_mode": "allocate", "release_authorized": True,
        "transport": "https", "target_identity_verified": True, "reconciliation_required": False,
    }
    path = canonical_claim_path("account")
    return SimpleNamespace(client=client, owner=owner, claim=claim, path=path, tmp=tmp_path)


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_public_client_binds_owner_without_disk_or_provider_access(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.read_text", lambda *a, **k: pytest.fail("construction read files"))
    client = AstridClient(remote=SimpleNamespace())
    assert runpod_generation_owner(client).client is client


@pytest.mark.integration
def test_public_open_and_launcher_cli_bind_owner_against_real_runtime(product, real_runtime, monkeypatch, capsys):
    from astrid.core.generation import cli
    from astrid.sdk.workspace_client import PROTOCOL

    daemon = real_runtime()
    with AstridClient.open(endpoint=daemon.endpoint, credential=daemon.credential_path,
            realm_id=daemon.service.realm["id"], actor_id="owner", client_name="product-test",
            client_version="stage1", protocol_version=PROTOCOL) as client:
        assert runpod_generation_owner(client).client is client
        assert GenerationFacade().describe(target="runpod", client=client)["state"] == "unclaimed"
    monkeypatch.setattr("astrid.sdk.autobootstrap.ensure_runtime", lambda **kwargs: {
        "status": "started", "endpoint": daemon.endpoint, "realm_id": daemon.service.realm["id"],
        "actor_id": "owner", "credential_file": str(daemon.credential_path),
    })
    assert cli.main(["describe", "--target", "runpod", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "unclaimed"


def test_describe_unclaimed_writes_nothing(product):
    before = snapshot(product.tmp)
    result = GenerationFacade().describe(target="runpod", client=product.client)
    assert result["state"] == "unclaimed" and result["released"] is False
    assert snapshot(product.tmp) == before and not product.path.parent.exists()


def test_describe_claim_never_exposes_transport_or_credential_paths(product):
    claim = {**product.claim, "https_token_file": "/private/secret", "api_key_ref": "PRIVATE_KEY"}
    write_target_record(product.path, claim)
    before = snapshot(product.tmp)
    result = GenerationFacade().describe(target="runpod", client=product.client)
    assert result["pod_id"] == "pod123" and result["released"] is False
    assert "private" not in json.dumps(result).lower()
    assert snapshot(product.tmp) == before


@pytest.mark.parametrize("change", [
    {"provider_account_ref": "foreign"}, {"worker_image": "foreign"}, {"network_volume_id": "foreign"},
    {"request_name": "foreign"}, {"acquisition_mode": "attach"}, {"gpu_type": "foreign"},
])
def test_foreign_custody_is_rejected_read_only(product, change):
    write_target_record(product.path, {**product.claim, **change})
    before = snapshot(product.tmp)
    with pytest.raises((ValueError, RuntimeError)):
        GenerationFacade().describe(target="runpod", client=product.client)
    assert snapshot(product.tmp) == before


def completed(claim):
    return {**claim, "state": "cleanup_complete", "cleanup_status": "complete",
        "release_quiescence": _quiescence(claim), "cleanup": {
            "status": "terminated", "pod_id": claim["pod_id"],
            "backup_volume_id": claim["network_volume_id"], "backup_volume_preserved": True,
            "absence": {"pod_id": claim["pod_id"], "provider_account_ref": claim["provider_account_ref"],
                "exact_get_status": 404, "account_list_succeeded": True, "account_list_absent": True}}}


def test_completed_release_needs_all_existing_proofs(product):
    write_target_record(product.path, completed(product.claim))
    before = snapshot(product.tmp)
    result = GenerationFacade().release(engine="comfyui", target="runpod", client=product.client)
    assert result["state"] == "cleanup_complete" and result["released"] is True
    assert snapshot(product.tmp) == before


@pytest.mark.parametrize("missing", ["release_quiescence", "cleanup", "get404", "list_absence", "volume", "pod"])
def test_completed_state_or_admission_is_never_release_proof(product, missing):
    record = completed(product.claim)
    if missing in {"release_quiescence", "cleanup"}:
        record.pop(missing)
    elif missing == "get404":
        record["cleanup"]["absence"]["exact_get_status"] = 200
    elif missing == "list_absence":
        record["cleanup"]["absence"]["account_list_absent"] = False
    elif missing == "volume":
        record["cleanup"]["backup_volume_preserved"] = False
    else:
        record["cleanup"]["pod_id"] = "foreign"
    write_target_record(product.path, record)
    before = snapshot(product.tmp)
    with pytest.raises((ValueError, RuntimeError)):
        GenerationFacade().release(engine="comfyui", target="runpod", client=product.client)
    assert snapshot(product.tmp) == before


def test_missing_f07_preparation_fails_before_claim_install_or_warm_hold(product):
    before = snapshot(product.tmp)
    with pytest.raises(GenerationReadinessError) as raised:
        product.owner.prepare(workflow=object(), inputs={}, consent=("paid",),
                              max_usd=2, deadline="2099-01-01T00:00:00Z")
    issue = raised.value.to_envelope()["issues"][0]
    assert issue["code"] == "missing_dependency"
    assert issue["details"]["missing_symbols"] == [
        "build_prepared_worker_selection", "GenerationReleaseOwner.release_generation"]
    assert snapshot(product.tmp) == before and not product.path.parent.exists()


def test_read_only_validation_uses_original_transport_over_actual_http(product, endpoint, network, local_tls):
    endpoint = replace(endpoint, lifecycle_owner=replace(endpoint.lifecycle_owner,
                                                        owner_id=product.claim["operation_id"]))
    write_target_record(product.path, {**product.claim, "managed_worker": {
        "state": "committed", "engine_endpoint": endpoint_to_dict(endpoint)}})
    before = snapshot(product.tmp)
    target = product.owner.validation_target()
    assert target.object_info() == network["objects"]
    assert all(path == "/object_info" for _method, path, _headers in network["upstream"])
    assert snapshot(product.tmp) == before
    target.endpoint_transport.close()


@pytest.mark.parametrize("field", ["pod", "account", "operation"])
def test_validation_denies_foreign_endpoint_before_http(product, endpoint, network, field):
    if field == "operation":
        endpoint = replace(endpoint, lifecycle_owner=replace(endpoint.lifecycle_owner, owner_id="foreign"))
    elif field == "account":
        endpoint = replace(endpoint, attestation=replace(endpoint.attestation, provider_account_ref="foreign"))
    # The unmodified fixture's operation is already foreign to the product
    # journal; change that to exercise the pod identity check independently.
    else:
        endpoint = replace(endpoint, lifecycle_owner=replace(endpoint.lifecycle_owner,
                                                            owner_id=product.claim["operation_id"]))
        product.claim["pod_id"] = "foreign"
    write_target_record(product.path, {**product.claim, "managed_worker": {
        "state": "committed", "engine_endpoint": endpoint_to_dict(endpoint)}})
    before = snapshot(product.tmp)
    with pytest.raises(GenerationReadinessError):
        product.owner.validation_target()
    assert not network["agent"] and not network["upstream"]
    assert snapshot(product.tmp) == before


def test_checkout_ensures_existing_owner_and_detaches_only(product, prepared, network, local_tls):
    before = prepared.claim_path.read_bytes()
    with product.owner.checkout_prepared_endpoint(prepared.inspector, prepared.host, **prepared.kwargs) as transport:
        assert transport.object_info(deadline=prepared.kwargs["deadline"]) == network["objects"]
    assert prepared.observed == [prepared.host]
    current = json.loads(prepared.claim_path.read_bytes())
    assert current["state"] == "claimed" and current["pod_id"] == prepared.claim["pod_id"]
    assert "engine_endpoint" in current["managed_worker"]
    assert prepared.claim_path.read_bytes() != before
