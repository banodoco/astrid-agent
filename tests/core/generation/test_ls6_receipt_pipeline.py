"""LS-2 counters over LS-6 custody, settlement and independent D14 delivery.

Inference is an attributed HTTP fixture. The download, VibeComfy collector,
Agent materializer/host, Runtime upload/settlement/stream and caller are real.
These are Python boundary counters, not native or latency qualification.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import json
import threading
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._local_speed_hashes import OutputHashObserver


def produce(tmp_path, data, monkeypatch, *, task_id="task-1", attempt_id="attempt-1", after_outputs=None):
    from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
    from astrid.core.generation.backends.vibecomfy_local_server import AttachedArtifacts
    from astrid.core.generation.producer_result import _read_correlated_result
    from vibecomfy.runtime.session import _build_managed_generation_result

    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    vibe_run = importlib.import_module("vibecomfy.runtime.run")
    run_dir = tmp_path / "producer"
    run_dir.mkdir(parents=True)
    descriptor = {"filename": "final.png", "subfolder": "", "type": "output", "animated": False}
    artifact = {"filename": "final.png", "reported_path": "final.png", "descriptor": descriptor}
    journal = {"prompt_id": "prompt-1", "queue_acceptance": {"status": "accepted", "prompt_id": "prompt-1"},
               "artifacts": [artifact]}
    (run_dir / "attempt.json").write_text(json.dumps(journal))
    calls = []
    class Response(io.BytesIO):
        status = 200
        headers = {"Content-Length": str(len(data))}
    def opener(request, **kwargs):
        calls.append(request.full_url)
        return Response(data)
    comfy = ComfyTargetClient(ComfyTarget("http://127.0.0.1:8188", "attached"), opener=opener)
    comfy.witness_prompt("prompt-1")
    retrieval = asyncio.run(AttachedArtifacts(comfy).download_artifacts(local_root=run_dir / "downloaded-artifacts"))
    assert vibe_run._bind_downloaded_lifecycle_artifacts([artifact], Path(retrieval["local_root"]),
        output_receipts=retrieval["output_receipts"], prompt_id="prompt-1") == 1
    metadata = {"run_id": "producer-1", "task_id": task_id, "attempt_id": attempt_id,
        "created": "2026-11-18T00:00:00Z", "prompt_id": "prompt-1", "queued": {"prompt_id": "prompt-1"},
        "inputs": {}, "artifacts": [artifact], "media_validation": {"status": "verified"},
        "declared_outputs": [{"name": "image", "mime_type": "image/png", "declaration_ordinal": 0}],
        "artifact_manifest": {"attribution": [{"path": "final.png", "output": "image"}]}}
    payload, unavailable = _build_managed_generation_result(run_dir=run_dir, metadata=metadata,
        attempt_bundle=journal, evidence={"queue_acceptance": journal["queue_acceptance"]})
    assert unavailable == {} and payload is not None
    envelope = run_dir / "managed-generation-result.json"
    envelope.write_text(json.dumps(payload))
    _read_correlated_result(envelope, task_id=task_id, attempt_id=attempt_id)
    spool = tmp_path / "host" / "outputs"
    manifest = run._materialize_managed_generation_result(spool,
        SimpleNamespace(managed_generation_result_path=envelope), task_identity=task_id,
        attempt_identity=attempt_id, workflow_path=tmp_path / "unused.json", after_outputs=after_outputs)
    return spool, manifest, calls


@pytest.mark.parametrize("size", [37, 9 * 1024 * 1024 + 13])
def test_one_fetch_one_producer_hash_and_independent_runtime_and_caller(tmp_path, monkeypatch, size):
    from astrid.core._shared.result_manifest import harvest_staged_outputs
    from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
    from astrid.core.generation.delivery import deliver_generation
    from astrid.sdk.remote import RemoteMedia
    from astrid.sdk.workspace_client import WorkspaceClient
    from runtime_protocol.daemon import RuntimeDaemon
    from runtime_protocol.store import RealmStore

    data = (b"attributed-output-" * ((size + 17) // 18))[:size]
    assert len(data) == size
    digest = hashlib.sha256(data).hexdigest()
    definition_digest = "sha256:" + hashlib.sha256(b"vibecomfy.run").hexdigest()
    root = tmp_path / "realm"
    RealmStore.initialize(root).close()
    daemon = RuntimeDaemon(root, support_root=tmp_path / "support").start()
    try:
        service = daemon.service
        project = service.create_project({"slug": "receipt", "name": "Receipt"}, idempotency_key="project")
        source = service.ingest(project["id"], b"admitted workflow", media_type="application/json",
                                idempotency_key="input")["data"]["digest"]
        token, _ = daemon.credentials.provision("receipt-worker", list(RuntimeProtocolClient.WORKER_SCOPES))
        worker = RuntimeProtocolClient(daemon.endpoint, token)
        worker.register_executor("receipt-worker", capabilities=[{"capability_id": "vibecomfy.run",
            "definition_digest": definition_digest, "status": "ready", "required_resource_keys": [],
            "estimated_scratch_bytes": 0, "estimated_output_bytes": size * 2 + 1024 * 1024}],
            max_concurrency=1, resource_keys=[], source_digest=None, runtime_epoch=service.health()["runtime_epoch"])
        effect = {"effect_type": "generation.create_with_variant", "target_id": project["id"], "payload": {
            "generation_type": "image", "metadata": {}, "variant_type": "original", "output_name": "vibecomfy_run",
            "output_ordinal": 0, "primary_policy": "preserve"}}
        service.create_task({"capability_id": "vibecomfy.run", "capability_digest": definition_digest,
            "project": project["id"], "input_object_ids": [source], "settlement_effect": effect,
            "idempotency_key": "primary"})
        claim = worker.claim_next(executor_id="receipt-worker", capability_ids=["vibecomfy.run"], idempotency_key="claim")
        claim = asdict(claim) if not isinstance(claim, dict) else claim
        observers = {name: OutputHashObserver({digest: size}) for name in ("producer", "runtime", "caller")}
        phase = ["producer"]
        main_thread = threading.get_ident()
        def selected():
            if phase[0] == "producer":
                return observers["producer"]
            return observers[phase[0] if threading.get_ident() == main_thread else "runtime"]
        monkeypatch.setattr(hashlib, "sha256", lambda *a, **kw: selected().sha256(*a, **kw))
        monkeypatch.setattr(hashlib, "new", lambda *a, **kw: selected().new(*a, **kw))
        monkeypatch.setattr(hashlib, "file_digest", lambda *a, **kw: selected().file_digest(*a, **kw))
        spool, manifest, calls = produce(tmp_path, data, monkeypatch,
            task_id=claim["task_id"], attempt_id=claim["attempt_id"])
        assert len(calls) == 1 and "/view?" in calls[0]
        declared = (SimpleNamespace(name="vibecomfy_run", path_template="outputs/final.png", artifact_type="image/png"),
                    SimpleNamespace(name="result_manifest", path_template="outputs/managed-generation-result.json", artifact_type="application/json"))
        harvested = harvest_staged_outputs(spool, declared_outputs=declared, use_output_receipts=True)
        host = object.__new__(GenericPackHost)
        host.client = worker
        host._validate_producer_evidence(harvested, output_root=spool, task_id=claim["task_id"],
                                         attempt_id=claim["attempt_id"], successful=True)
        record = SimpleNamespace(definition=SimpleNamespace(outputs=declared, metadata={}))
        typed = host._typed_outputs(record, harvested, spool.parent, defer_content_check_to_runtime=True)
        assert observers["producer"].snapshot().completed_streams == 1
        # Client-side upload remains producer work; server request threads are
        # counted separately while receiving/verifying actual upload bytes.
        phase[0] = "producer_upload"
        observers["producer_upload"] = observers["producer"]
        outputs = host._upload_outputs(typed, project_id=project["id"], run_id=claim["run_id"],
            task_id=claim["task_id"], attempt_id=claim["attempt_id"], lease_id=claim["lease_id"],
            fence=claim["fence"], runtime_epoch=claim["runtime_epoch"])
        result = worker.generated.settle_attempt(claim["attempt_id"], {"effect": effect, "outputs": outputs,
            **{key: claim[key] for key in ("lease_id", "fence", "runtime_epoch")}}, idempotency_key="settle")
        assert result["state"] == "completed"
        rows = service.managed_outputs(claim["task_id"])
        primary = next(row for row in rows if row["role"] == "result")
        envelope = next(row for row in rows if row["role"] == "manifest")
        assert result["result"]["thumbnail_intents"][0]["source_object_id"] == "sha256:" + digest
        before_delivery = observers["runtime"].snapshot()
        opens = []
        original_open = service.open_object
        def opened(object_id, **kwargs):
            if object_id.removeprefix("sha256:") == digest:
                opens.append(object_id)
            return original_open(object_id, **kwargs)
        monkeypatch.setattr(service, "open_object", opened)
        phase[0] = "caller"
        client = SimpleNamespace(media=RemoteMedia(WorkspaceClient(daemon.endpoint, daemon.token)))
        delivered = deliver_generation({"task_id": claim["task_id"], "project_id": project["id"],
            "manifest_ref": envelope["object_id"], "manifest": {"outputs": [primary]}},
            client=client, out=tmp_path / "delivered.png", project=project["id"])
        caller_file = Path(delivered["image_paths"][0])
        assert caller_file.read_bytes() == data and len(opens) == 1
        assert observers["producer"].snapshot().completed_streams == 1
        assert observers["producer"].snapshot().bytes_hashed == size
        assert observers["caller"].snapshot().completed_streams == 1
        assert observers["caller"].snapshot().bytes_hashed == size
        assert before_delivery.completed_streams >= 1
        assert observers["runtime"].snapshot().completed_streams - before_delivery.completed_streams == 1
        assert all(not observer.snapshot().coverage_gaps for observer in observers.values())
        caller_file.write_bytes(b"caller mutation")
        stored = service.cas.root / digest[:2] / digest[2:]
        assert stored.read_bytes() == data
        print(json.dumps({"bytes": size, "fetches": len(calls), "runtime_before_delivery": asdict(before_delivery),
            **{name: asdict(observers[name].snapshot()) for name in ("producer", "runtime", "caller")}}, sort_keys=True))
    finally:
        daemon.stop()


def test_transforming_hook_mints_a_fresh_receipt(tmp_path, monkeypatch):
    data = b"original attributed bytes"
    changed = b"hook transformed output"
    def transform(paths):
        paths[0].write_bytes(changed)
        return paths
    spool, manifest, calls = produce(tmp_path, data, monkeypatch, after_outputs=transform)
    output = next(row for row in manifest["outputs"] if row["role"] == "result")
    receipt = output["output_receipt"]
    assert len(calls) == 1 and (spool / output["path"]).read_bytes() == changed
    assert receipt["producer"] == "transform"
    assert receipt["sha256"] == hashlib.sha256(changed).hexdigest()
    assert receipt["source_sha256"] == hashlib.sha256(data).hexdigest()
    assert receipt["bytes"] == len(changed)


def test_changed_file_cannot_reuse_a_producer_receipt(tmp_path, monkeypatch):
    from astrid.core._shared.result_manifest import HarvestError, harvest_staged_outputs
    spool, manifest, _ = produce(tmp_path, b"untouched output", monkeypatch)
    output = next(row for row in manifest["outputs"] if row["role"] == "result")
    (spool / output["path"]).write_bytes(b"x" * output["bytes"])
    with pytest.raises(HarvestError, match="hash|digest"):
        harvest_staged_outputs(spool, use_output_receipts=True)


def test_caller_refuses_changed_in_process_verified_file(tmp_path):
    from astrid.core.generation.delivery import deliver_generation
    from tests.core.generation.test_output_delivery import settled_media
    runtime = settled_media(filenames=("one.png",))
    original = runtime.client.media._open_delivery
    def tampered(*args, **kwargs):
        result = original(*args, **kwargs)
        if result.ok:
            path = Path(result.data["local_path"])
            path.write_bytes(b"x" * path.stat().st_size)
        return result
    runtime.client.media._open_delivery = tampered
    out = tmp_path / "prior.png"
    out.write_bytes(b"prior caller file")
    with pytest.raises(ValueError, match="delivery failed"):
        deliver_generation(runtime.payload, client=runtime.client, out=out)
    assert out.read_bytes() == b"prior caller file"
    assert not list(tmp_path.glob(".astrid-delivery-*"))
