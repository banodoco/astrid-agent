"""Producer -> host harvest -> Runtime CAS, across terminal state and restart."""
from __future__ import annotations

import hashlib
import importlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from astrid.core._shared.result_manifest import ResultManifestError, harvest_staged_outputs
from astrid.core.contracts.managed_generation_result import ManagedGenerationResultError
from astrid.core.execution.generic_host import GenericPackHost, HostError, RuntimeProtocolClient
from runtime_protocol.service import RuntimeService
from runtime_protocol.store import RealmStore
from tests.test_producer_evidence_f06 import _managed_payload, _write_production


def _materialize(tmp_path, monkeypatch, *, status="succeeded", task_id="task-1", attempt_id="attempt-1"):
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    producer = tmp_path / "producer"
    producer.mkdir()
    payload = _managed_payload(producer, execution_status=status)
    media = producer / "outputs" / "final.png"
    Image.new("RGB", (1, 1), (20, 50, 80)).save(media)
    payload.update(task_id=task_id, attempt_id=attempt_id)
    payload["outputs"][0].update(path="outputs/final.png", media_type="image/png",
                                 bytes=media.stat().st_size, sha256=hashlib.sha256(media.read_bytes()).hexdigest())
    spool = tmp_path / "spool"
    run._materialize_managed_generation_result(
        spool, _write_production(producer, payload), task_identity=task_id,
        attempt_identity=attempt_id, workflow_path=tmp_path / "unused-workflow.json",
    )
    return spool


def _record():
    return SimpleNamespace(definition=SimpleNamespace(outputs=[
        SimpleNamespace(name="vibecomfy_run", artifact_type=None),
        SimpleNamespace(name="result_manifest", artifact_type=None),
    ]))


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_real_runtime_retains_source_settings_and_partial_media_after_cleanup_restart(tmp_path, monkeypatch, status):
    realm = tmp_path / "realm"
    RealmStore.initialize(realm).close()
    service = RuntimeService(realm)
    try:
        capability = "f06.fixture"
        digest = "sha256:" + "2" * 64
        project = service.create_project({"slug": "f06", "name": "F06"}, idempotency_key="project")
        service.register_capability({"capability_id": capability, "definition_digest": digest})
        service.register_executor({"executor_id": "f06-worker", "capabilities": [capability]}, idempotency_key="worker")
        created = service.create_task({"capability_id": capability, "capability_digest": digest,
                                       "project": project["id"], "spec": {}, "idempotency_key": "task"})
        attempt = service.claim_next({"executor_id": "f06-worker", "capability_ids": [capability],
                                      "runtime_epoch": service.health()["runtime_epoch"]}, idempotency_key="claim")
        task_id, attempt_id = created["task"]["id"], attempt["attempt_id"]
        spool = _materialize(tmp_path, monkeypatch, status=status, task_id=task_id, attempt_id=attempt_id)

        class Generated:
            timeout = 30
            calls = []

            def health(self):
                return service.health()

            def get_task(self, task_id):
                task = service.task(task_id)["task"]
                return {**task, "version": int(task["attempt"]) + 1}

            def settle_attempt(self, attempt_id, body, *, idempotency_key):
                self.calls.append(("settle", attempt_id, body, idempotency_key))
                return service.settle_attempt(attempt_id, body, idempotency_key=idempotency_key)

            def fail_attempt(self, attempt_id, *, idempotency_key, **body):
                self.calls.append(("fail", attempt_id, body, idempotency_key))
                return service.fail_attempt(attempt_id, body, idempotency_key=idempotency_key)

            def cancel_task(self, task_id, *, idempotency_key, **body):
                self.calls.append(("cancel", task_id, body, idempotency_key))
                return service.cancel_task_canonical(task_id, body, idempotency_key=idempotency_key)

        protocol = RuntimeProtocolClient.__new__(RuntimeProtocolClient)
        protocol.generated = Generated()
        protocol._attempt_runtime_epochs = {attempt_id: attempt["runtime_epoch"]}
        host = GenericPackHost.__new__(GenericPackHost)
        host.client = protocol
        descriptors = harvest_staged_outputs(spool)
        host._validate_producer_evidence(descriptors, output_root=spool, task_id=task_id, attempt_id=attempt_id)
        typed = host._typed_outputs(_record(), descriptors, spool)
        outputs = host._upload_outputs(typed, project_id=project["id"], run_id=created["run"]["id"],
                                       task_id=task_id, attempt_id=attempt_id, lease_id=attempt["lease_id"],
                                       fence=attempt["fence"], runtime_epoch=attempt["runtime_epoch"])
        manifest = next(row for row in outputs if row["name"] == "result_manifest")
        result = {"manifest_ref": {"object_id": manifest["digest"], "size": manifest["size"]}}
        fence = {"attempt_id": attempt_id, "fence": attempt["fence"]}
        if status == "succeeded":
            protocol.settle(task_id, attempt["lease_id"], result=result, outputs=outputs, effect=None, **fence)
        elif status == "failed":
            protocol.fail(task_id, attempt["lease_id"], "producer failed", result=result, outputs=outputs, **fence)
        else:
            protocol.cancel(task_id, lease_id=attempt["lease_id"], result=result, outputs=outputs,
                            stop_observed=True, **fence)
        mutation, identity, body, key = protocol.generated.calls[0]
        shutil.rmtree(spool)
        shutil.rmtree(tmp_path / "producer")
        service.close()
        service = RuntimeService(realm)
        # Replaying the exact terminal request after scratch deletion must be
        # a read of the existing publication, with no duplicate associations.
        if mutation == "settle":
            service.settle_attempt(identity, body, idempotency_key=key)
        elif mutation == "fail":
            service.fail_attempt(identity, body, idempotency_key=key)
        else:
            service.cancel_task_canonical(identity, body, idempotency_key=key)
        stored = service.task(task_id)["task"]
        assert stored["status"] == {"succeeded": "completed", "failed": "failed", "cancelled": "cancelled"}[status]
        assert stored["result"]["manifest_ref"] == result["manifest_ref"]
        associations = service.managed_outputs(task_id)
        assert len(associations) == 4
        assert len({row["association_id"] for row in associations}) == 4
        envelope = json.loads(service.cas.path_for(manifest["digest"].removeprefix("sha256:")).read_bytes())
        assert envelope["outcomes"]["execution"]["status"] == status
        for field, expected in (("source", b"\x00source\xff"), ("effective_settings", b'{"seed":7}\n')):
            ref = envelope["inputs"][field]
            assert service.cas.path_for(ref["sha256"]).read_bytes() == expected
            assert ref["object_id"] == "sha256:" + hashlib.sha256(expected).hexdigest()
        assert service.store.conn.execute("SELECT count(*) FROM generation_variants").fetchone()[0] == 0
    finally:
        service.close()


@pytest.mark.parametrize("field", ["task_id", "attempt_id"])
def test_success_harvest_rejects_mismatched_receipt_identity(tmp_path, monkeypatch, field):
    spool = _materialize(tmp_path, monkeypatch)
    host = GenericPackHost.__new__(GenericPackHost)
    expected = {"task_id": "task-1", "attempt_id": "attempt-1", field: "stale"}
    with pytest.raises(ManagedGenerationResultError, match=field):
        host._validate_producer_evidence(harvest_staged_outputs(spool), output_root=spool, **expected)


def test_failed_partial_result_cannot_satisfy_successful_publication(tmp_path, monkeypatch):
    spool = _materialize(tmp_path, monkeypatch, status="failed")
    host = GenericPackHost.__new__(GenericPackHost)
    with pytest.raises(HostError, match="successful publication"):
        host._validate_producer_evidence(harvest_staged_outputs(spool), output_root=spool,
                                         task_id="task-1", attempt_id="attempt-1", successful=True)


def test_host_adds_installed_manifest_without_development_lookup(tmp_path, monkeypatch):
    spool = _materialize(tmp_path, monkeypatch)
    installed = tmp_path / "installed" / "boot-manifest.json"
    installed.parent.mkdir()
    installed.write_bytes(b'{"installed":"product"}\n')
    host = GenericPackHost.__new__(GenericPackHost)
    host.boot_manifest_path = installed
    host.boot_manifest_provenance = lambda: {"kind": "astrid.boot_manifest", "sha256": "sha256:" + "a" * 64}
    host._prepare_producer_evidence(spool, {"execution_request": {"target": {"kind": "local"}}})
    receipt = json.loads((spool / "manifest.json").read_text())
    envelope = receipt["managed_generation_result"]
    reference = envelope["evidence"]["producer"]["build"]["installed_product"]
    assert reference["sha256"] == hashlib.sha256(installed.read_bytes()).hexdigest()
    assert envelope["evidence"]["transport"]["target"] == {"kind": "local"}
    descriptors = harvest_staged_outputs(spool)
    host._validate_producer_evidence(descriptors, output_root=spool, task_id="task-1", attempt_id="attempt-1")
    first = (spool / "manifest.json").read_bytes()
    host._prepare_producer_evidence(spool, {"execution_request": {"target": {"kind": "local"}}})
    assert (spool / "manifest.json").read_bytes() == first


@pytest.mark.parametrize("mismatch", [False, True])
def test_host_restores_admitted_source_identity_after_bundle_staging(tmp_path, monkeypatch, mismatch):
    spool = _materialize(tmp_path, monkeypatch)
    host = GenericPackHost.__new__(GenericPackHost)
    host.boot_manifest_provenance = lambda: None
    digest = hashlib.sha256(b"different" if mismatch else b"\x00source\xff").hexdigest()
    source = {"digest": "sha256:" + digest, "sha256": digest,
              "bytes": len(b"\x00source\xff"), "filename": "submitted-original.json"}
    task = {"spec": {"spec": {"inputs": {"source": source}}}}
    if mismatch:
        with pytest.raises(HostError, match="differs from admitted source"):
            host._prepare_producer_evidence(spool, task)
        return
    host._prepare_producer_evidence(spool, task)
    receipt = json.loads((spool / "manifest.json").read_bytes())
    retained = receipt["managed_generation_result"]["inputs"]["source"]
    assert all(retained[key] == value for key, value in source.items())
    assert retained["object_id"] == source["digest"]
    host._validate_producer_evidence(harvest_staged_outputs(spool), output_root=spool,
                                     task_id="task-1", attempt_id="attempt-1")


def test_host_refuses_to_rehash_corrupt_producer_manifest(tmp_path, monkeypatch):
    spool = _materialize(tmp_path, monkeypatch)
    manifest = spool / "outputs" / "managed-generation-result.json"
    payload = json.loads(manifest.read_text())
    payload["evidence"]["producer"]["engine"] = "changed"
    manifest.write_text(json.dumps(payload))
    host = GenericPackHost.__new__(GenericPackHost)
    host.boot_manifest_provenance = lambda: None
    with pytest.raises(ResultManifestError, match="hashes to"):
        host._prepare_producer_evidence(spool, {})


@pytest.mark.parametrize("lost_after_commit", [False, True])
def test_success_outage_retries_exact_publication_or_reads_committed_result(lost_after_commit):
    from tests.core.execution.test_terminal_evidence_transport_f05 import _client

    result = {"manifest_ref": {"object_id": "sha256:" + "d" * 64, "size": 123}}
    protocol = _client(None)
    protocol._claimed_runtime_epoch = lambda attempt_id: 9
    writes = []

    def settle(attempt_id, body, *, idempotency_key):
        writes.append((attempt_id, body, idempotency_key))
        if len(writes) == 1:
            if lost_after_commit:
                protocol.generated.task_result = result
            raise OSError("Runtime response lost")
        protocol.generated.task_result = result
        return {"task": {"id": "task-1", "result": result}}

    protocol.generated.settle_attempt = settle
    protocol.settle("task-1", "lease-1", result=result, outputs=[], effect=None, attempt_id="attempt-1", fence=3)
    assert len(writes) == (1 if lost_after_commit else 2)
    assert all(write == writes[0] for write in writes)


def test_unacknowledged_success_retains_spool_without_publishing_failure(host_case, monkeypatch):
    from astrid.core._shared.result_manifest import build_manifest, write_manifest

    case = host_case
    pack = case.host.capabilities["test.echo"].manifest_path
    definition = json.loads(pack.read_text())
    definition["outputs"].append({"name": "result_manifest", "type": "file"})
    pack.write_text(json.dumps(definition))
    case.host.discover()
    case.host.attempt_root = None
    allocate_root = case.host._allocate_attempt_root

    def allocate(*args):
        case.root = allocate_root(*args)
        return case.root

    monkeypatch.setattr(case.host, "_allocate_attempt_root", allocate)

    def execute(record, inputs, output_root, attempt, **kwargs):
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "answer.txt").write_bytes(b"complete")
        (output_root / "durable.json").write_text('{"kind":"generic-result"}')
        write_manifest(output_root / "manifest.json", build_manifest(
            kind="test.echo", inputs={}, created="2026-11-01T00:00:00Z", warnings=[],
            outputs=[{"name": "answer", "path": "answer.txt", "ordinal": 0, "role": "result"},
                     {"name": "result_manifest", "path": "durable.json", "ordinal": 1,
                      "role": "manifest", "media_type": "application/json"}],
        ))
        return SimpleNamespace(payload={}, process_id=12345, returncode=0)

    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    monkeypatch.setattr(case.runtime, "settle", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("ACK unavailable")))
    with pytest.raises(OSError, match="ACK unavailable"):
        case.host.run_task(case.task, lease_token="lease-result")
    assert not case.runtime.failures
    assert case.root.exists()
    assert case.host.last_cleanup_receipt["intended_disposition"] == "retained"


# Reuse the real host fixture for the publication-retention regression above.
from tests.test_generic_host_result_json import host_case  # noqa: E402, F401
