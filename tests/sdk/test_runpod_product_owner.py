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


def test_public_release_verb_dispatches_to_bound_runpod_owner(product):
    before = snapshot(product.tmp)
    result = product.client.release_generation(engine="comfyui", target="runpod", wait=True)
    assert result["state"] == "unclaimed" and result["released"] is False
    assert snapshot(product.tmp) == before


def test_original_https_token_reference_survives_lost_create_reconnect(product, monkeypatch):
    from astrid.packs.runpod.target_selection import RunPodOffer, read_runpod_preferences
    from astrid.packs.runpod.worker_preparation import target_owner

    raw = read_runpod_preferences().as_mapping()
    raw["credential_ref"] = "env:FAKE_PRODUCT_RUNPOD_CREDENTIAL"
    monkeypatch.setenv("FAKE_PRODUCT_RUNPOD_CREDENTIAL", "product-test-value")
    save_runpod_preferences(raw)
    preferences = read_runpod_preferences()
    offer = RunPodOffer("account", "gpu", 1, "test-offer")
    first = product.owner._provider_config(preferences, offer)
    with target_owner(product.path):
        metadata = product.owner._transport_metadata(preferences, first)
        write_target_record(product.path, {**product.claim, **metadata,
            "state": "allocation_unknown", "schema_version": "astrid.runpod.allocation-attempt.v1"})
    before = snapshot(product.tmp)
    second = product.owner._provider_config(preferences, offer)
    with target_owner(product.path):
        assert product.owner._transport_metadata(preferences, second) == metadata
    assert first.https_token == second.https_token
    assert snapshot(product.tmp) == before
    assert first.api_key not in product.path.read_text()


def test_cold_public_image_planning_is_read_only_and_has_no_endpoint_approval(product, monkeypatch):
    from astrid.core.generation import entries
    from vibecomfy.nodes.core import EmptyImage, SaveImage
    from vibecomfy.templates import ReadyMetadata, new_workflow
    from vibecomfy.workflow_bundle import emit_bundle

    source = product.tmp / "cold-image.py"
    workflow = new_workflow(ReadyMetadata.build(capability="runtime_smoke"), source_path=str(source))
    image = EmptyImage(_id="1", width=64, height=64, batch_size=1, color=16711680)
    save = SaveImage(_id="2", images=image, filename_prefix="runpod-cold-image")
    workflow.finalize({}, output_node=save, output_type="SaveImage", name="image",
        artifact_kind="image", mime_type="image/png", expected_cardinality="one",
        filename_prefix="runpod-cold-image")
    bundle = emit_bundle(workflow, source, {"operation": "authored"})
    (source.parent / "source.json").write_text(json.dumps(bundle.materialize_ui(
        schema_provider=entries._schema_provider(), strict=True)))
    entry = {"id": workflow.id, "kind": "image", "modes": ["t2i"], "bundle": str(source),
             "binds": {}, "defaults": {}, "needs": []}
    monkeypatch.setattr(entries, "user_entries", lambda: [entry])
    monkeypatch.setattr(entries, "ready_entries", lambda: [])
    monkeypatch.setattr(entries, "action_entries", lambda: [])
    before = snapshot(product.tmp)
    result = GenerationFacade().image(entry=workflow.id, target="runpod", client=product.client,
        dry_run=True, consent=("paid",), max_usd=2, deadline="2099-01-01T00:00:00Z")
    assert result["status"] in {"ready", "ready_with_warnings"}
    assert result["target"]["placement"] == "runpod"
    assert "approval" not in result["entry"]
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


@pytest.mark.integration
@pytest.mark.parametrize("active", [False, True])
def test_paid_expiry_cancels_exact_runtime_task_before_release_attempt(product, real_runtime, active):
    from datetime import datetime, timedelta, timezone
    from runtime_protocol.errors import AuthorizationError, ConflictError
    from runtime_protocol.remote_worker_deployment import deployment_binding_from_task
    from astrid.packs.runpod.prepared_task import _claim_target
    from astrid.sdk.workspace_client import PROTOCOL

    daemon = real_runtime()
    service = daemon.service
    digest = "sha256:" + "b" * 64
    service.register_capability({"capability_id": "vibecomfy.run", "definition_digest": digest})
    service.register_executor({"executor_id": "pod-test-host", "capabilities": ["vibecomfy.run"],
                               "max_concurrency": 1}, idempotency_key="register-test-host")
    admitted = service.create_task({"capability_id": "vibecomfy.run", "capability_digest": digest,
        "input_object_ids": [], "spec": {"inputs": {}}, "execution_request": {
            "target": _claim_target(product.claim), "remote_activation_required": True,
            "lifecycle": {"mode": "leave_running"}}, "idempotency_key": "expiry-task"},
        enforce_readiness=True)
    task_id, run_id = admitted["task"]["id"], admitted["run"]["id"]
    if active:
        target = _claim_target(product.claim)
        binding = deployment_binding_from_task(service._task_resource(service.task(task_id)))
        epoch = service.health()["runtime_epoch"]
        evidence = "sha256:" + "c" * 64
        qualification = service.record_remote_activation(task_id, {
            "activation_id": "expiry-activation", "task_id": task_id, "run_id": run_id,
            "credential_actor": "pod-test-host", "binding_digest": binding.digest(),
            "deployment_digest": digest, "evidence_digest": evidence, "observation_digest": digest,
            "effective_target": target, "executor_incarnation": "pod-test-incarnation",
            "runtime_session_id": service.runtime_session_id, "runtime_epoch": epoch,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "authorized_child_lineage": {"parent_task_id": task_id, "parent_run_id": run_id,
                "max_depth": 1, "placement_version": 0, "effective_target": target},
        }, identity={"actor": "owner", "scopes": ["admin"]})
        identity = {"actor": "pod-test-host", "scopes": ["worker:execute"],
            "qualified_activation": qualification, "execution_binding": {
                "actual": target, "executor_incarnation": "pod-test-incarnation", "verification": {
                    "method": "credential_claim", "verified": True, "evidence_digest": evidence}}}
        attempt = service.claim_next({"executor_id": "pod-test-host", "capability_ids": ["vibecomfy.run"],
            "runtime_epoch": epoch, "target": target}, idempotency_key="expiry-claim", identity=identity)
        assert service._task_resource(service.task(task_id))["state"] == "running"
    write_target_record(product.path, {**product.claim, "managed_worker": {
        "activation_qualification": {"task_id": task_id, "run_id": run_id}}})
    with AstridClient.open(endpoint=daemon.endpoint, credential=daemon.credential_path,
            realm_id=service.realm["id"], actor_id="owner", client_name="expiry-test",
            client_version="stage1", protocol_version=PROTOCOL) as client:
        owner = runpod_generation_owner(client)
        request = {**product.claim, "task_id": task_id, "run_id": run_id,
                   "idempotency_key": "runpod-release:" + product.claim["operation_id"]}
        # No selection was published in this test. That blocks the provider
        # release, after Runtime has fenced/cancelled the exact admission.
        with pytest.raises(GenerationReadinessError) as raised:
            owner._expire(request)
        assert raised.value.to_envelope()["issues"][0]["code"] == "missing_dependency"
        task = client.tasks.show(task_id).data
        if active:
            assert task["state"] == "failed" and task["result"]["disposition"] == "lost"
            assert task["result"]["remote_stop_confirmed"] is False
            assert task["result"]["billing_stop_confirmed"] is False
        else:
            assert task["state"] == "cancelled"
            assert task["result"]["attempted"] is False
            assert task["result"]["cancel"]["requested"] is True
        assert not task["result"].get("outputs")
        if active:
            with pytest.raises((AuthorizationError, ConflictError)):
                service.heartbeat_attempt(attempt["attempt_id"],
                    {key: attempt[key] for key in ("lease_id", "fence", "runtime_epoch")},
                    idempotency_key="expiry-stale-heartbeat", identity=identity)
    saved = json.loads(product.path.read_bytes())
    assert saved["state"] == "cleanup_pending" and saved["reconciliation_required"] is True


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


def test_missing_prepared_artifact_fails_before_claim_install_or_warm_hold(product):
    before = snapshot(product.tmp)
    with pytest.raises(GenerationReadinessError) as raised:
        product.owner.prepare(workflow=object(), inputs={}, consent=("paid",),
                              max_usd=2, deadline="2099-01-01T00:00:00Z")
    issue = raised.value.to_envelope()["issues"][0]
    assert issue["code"] == "missing_dependency"
    assert issue["details"]["required_artifact"] == "prepared-worker.json"
    assert issue["details"]["consumer"] == "astrid.packs.runpod.prepared_task.load_prepared_worker_selection"
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
