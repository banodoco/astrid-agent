"""CAS reuse through real claim, input staging and output publication."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid_runtime_client import WorkspaceClient
from astrid_runtime_client.generated import ApiError

pytestmark = pytest.mark.integration

PAYLOAD = Path(__file__).parents[1] / "fixtures" / "cas_metadata" / "z_image_turbo.py"
DIGEST = "sha256:16302edf435877823f169b730d1f16f31a695fec5474e80a9dbec4db04c95647"


def _host(tmp_path, daemon):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "executor.yaml").write_text(json.dumps({
        "schema_version": 1, "id": "cas.evidence", "name": "CAS evidence",
        "kind": "external", "version": "1.0",
        "inputs": [{"name": "source", "type": "file", "required": True}],
        "command": {"argv": ["{python_exec}", "-c",
            "from pathlib import Path; "
            "Path('{out}/evidence-source-source.json').write_bytes(Path('{source}').read_bytes())"]},
        "outputs": [{"name": "evidence", "type": "file",
            "path_template": "{out}/evidence-source-source.json", "artifact_type": "application/json"}],
        "metadata": {"adapter_family": "cpu", "resource_keys": ["cpu"]},
    }))
    from runtime_protocol.daemon import WORKER_SCOPES
    token, _ = daemon.credentials.provision("cas-host", list(WORKER_SCOPES))
    host = GenericPackHost(pack_roots=[pack],
        client=RuntimeProtocolClient(daemon.endpoint, token), executor_id="cas-host")
    host.register()
    owner = WorkspaceClient(daemon.endpoint, daemon.token)
    owner.handshake("cas-metadata-test", "1", ["projects:read", "objects:write", "worker:execute"])
    project = owner.create_project("CAS reuse", slug="cas-reuse", idempotency_key="cas-project")
    payload = PAYLOAD.read_bytes()
    assert len(payload) == 3599 and "sha256:" + hashlib.sha256(payload).hexdigest() == DIGEST
    owner.ingest_project_object(project.project_id, payload, media_type="text/x-python",
        filename="z_image_turbo.py", idempotency_key="cas-source")
    record = host.capabilities["cas.evidence"]
    task = owner.admit_task(capability_id=record.id, capability_digest=record.capability_digest,
        input_object_ids=[DIGEST], project_id=project.project_id,
        spec={"inputs": {"source": {"digest": DIGEST, "filename": "source.json"}}},
        idempotency_key="cas-task")
    return host, owner, task


@pytest.mark.parametrize("inline", [True, False], ids=["inline", "bound-upload"])
def test_recorded_python_bytes_publish_as_json_evidence(tmp_path, real_runtime, inline):
    daemon = real_runtime()
    host, owner, task = _host(tmp_path, daemon)
    host.client.INLINE_SETTLEMENT_OUTPUTS = inline
    try:
        result = host.claim_once()
        assert result.state == "succeeded"
        completed = owner.get_task(task.task_id)
        assert completed.state == "succeeded"
        outputs, cursor = owner.list_managed_outputs(task.task_id)
        assert cursor is None and len(outputs) == 1
        assert outputs[0].object_id == DIGEST
        assert outputs[0].filename == "evidence-source-source.json"
        assert outputs[0].media_type == "application/json"
        assert owner.get_object(DIGEST).data == PAYLOAD.read_bytes()
        row, _ = daemon.service.object(DIGEST)
        assert (row["original_name"], row["media_type"]) == ("z_image_turbo.py", "text/x-python")
    finally:
        host.shutdown()
        daemon.stop()


def test_real_content_conflict_fails_claimed_task_without_observer_timeout(tmp_path, real_runtime, monkeypatch):
    daemon = real_runtime()
    host, owner, task = _host(tmp_path, daemon)
    upload = host._upload_outputs

    def corrupt_descriptor(*args, **kwargs):
        outputs = upload(*args, **kwargs)
        # Same claimed digest/length, different inline bytes: Runtime must refuse.
        import base64
        outputs[0]["data_base64"] = base64.b64encode(b"x" * 3599).decode()
        return outputs

    monkeypatch.setattr(host, "_upload_outputs", corrupt_descriptor)
    try:
        started = time.monotonic()
        with pytest.raises(ApiError, match="content hash does not match declared digest"):
            host.claim_once()
        assert time.monotonic() - started < 15
        failed = owner.get_task(task.task_id)
        assert failed.state == "failed"
        assert "content hash does not match declared digest" in failed.result["error"]["message"]
        outputs, _ = owner.list_managed_outputs(task.task_id)
        assert outputs == []
        assert owner.get_object(DIGEST).data == PAYLOAD.read_bytes()
    finally:
        host.shutdown()
        daemon.stop()
