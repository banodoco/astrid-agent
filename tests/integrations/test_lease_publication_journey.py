"""Lost renewal replies through normal local execution and durable publication."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid_runtime_client import ApiError, WorkspaceClient
from tests.helpers.runtime import vendored_runtime_commit

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


def test_committed_heartbeat_lost_reply_settles_durably(tmp_path: Path, real_runtime, monkeypatch):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "executor.yaml").write_text(json.dumps({
        "schema_version": 1, "id": "test.lease-journey", "name": "Lease Journey",
        "kind": "external", "version": "1.0",
        "command": {"argv": ["{python_exec}", "-c",
                              "import time; from pathlib import Path; "
                              "Path(r'{out}/started').write_text('started'); "
                              "time.sleep(36); Path(r'{out}/answer.txt').write_text('complete')"]},
        "outputs": [{"name": "answer", "type": "file", "path_template": "{out}/answer.txt"}],
        "metadata": {"adapter_family": "cpu", "resource_keys": ["cpu"]},
    }), encoding="utf-8")
    daemon = real_runtime(commit=vendored_runtime_commit())
    try:
        reader = WorkspaceClient(daemon.endpoint, daemon.token)
        reader.handshake("lease-journey", "1", ["projects:read", "worker:execute"])
        client = RuntimeProtocolClient(daemon.endpoint, daemon.worker_token)
        attempt_root = tmp_path / "attempt"
        host = GenericPackHost(pack_roots=[pack], client=client, attempt_root=attempt_root)
        record = host.discover()[0]
        host.register()
        project = reader.create_project("Lease journey", slug="lease-journey", idempotency_key="lease-journey-project")
        task = reader.admit_task(capability_id=record.id, capability_digest=record.capability_digest,
                                 project_id=project.project_id, input_object_ids=[],
                                 idempotency_key="lease-journey-task")
        original_request = type(client.generated)._request
        committed = []

        def drop_reply(transport, method, path, **kwargs):
            response = original_request(transport, method, path, **kwargs)
            if path.endswith("/heartbeat") and (attempt_root / "outputs" / "started").exists():
                assert response[0] == 200
                payload = json.loads(response[2])["data"]
                committed.append(payload)
                raise ApiError(0, "transport_timeout", "reply dropped after heartbeat commit")
            return response

        monkeypatch.setattr(type(client.generated), "_request", drop_reply)
        started = time.monotonic()
        result = host.run(once=True)
        assert time.monotonic() - started > 30
        assert committed and len(committed) >= 5
        assert len(result) == 1 and result[0].state == "succeeded"
        durable = reader.get_task(task.task_id)
        assert durable.state == "succeeded" and durable.result
        assert durable.attempt_id == committed[-1]["attempt_id"]
        assert durable.result["outputs"]
        receipt = json.loads((attempt_root / "result.json").read_text())
        assert receipt["status"] == "succeeded"
        assert receipt["publication"]["status"] == "settled"
        assert (attempt_root / "outputs" / "answer.txt").read_text() == "complete"
    finally:
        daemon.stop()


@pytest.mark.parametrize("disposition", ["cancelled", "recovered"])
def test_pending_publication_consumes_runtime_cleared_owner(tmp_path: Path, real_runtime, monkeypatch, disposition):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "executor.yaml").write_text(json.dumps({
        "schema_version": 1, "id": "test.cleared-owner", "name": "Cleared owner",
        "kind": "external", "version": "1.0",
        "command": {"argv": ["{python_exec}", "-c", "print('complete')"]},
        "metadata": {"adapter_family": "cpu", "resource_keys": ["cpu"]},
    }), encoding="utf-8")
    daemon = real_runtime(commit=vendored_runtime_commit())
    try:
        reader = WorkspaceClient(daemon.endpoint, daemon.token)
        reader.handshake("cleared-owner", "1", ["projects:read", "worker:execute"])
        client = RuntimeProtocolClient(daemon.endpoint, daemon.worker_token)
        root = tmp_path / "attempt"
        host = GenericPackHost(pack_roots=[pack], client=client, attempt_root=root)
        record = host.discover()[0]
        host.register()
        admitted = reader.admit_task(capability_id=record.id, capability_digest=record.capability_digest,
                                     input_object_ids=[], idempotency_key="cleared-owner-task")
        terminals, publications = [], []
        original_impl = host._run_task_impl

        def execute(*args, _terminal, **kwargs):
            terminals.append(_terminal)
            return original_impl(*args, _terminal=_terminal, **kwargs)

        def unavailable(*args, **kwargs):
            publications.append((args, kwargs))
            assert len(publications) == 1
            if disposition == "cancelled":
                reader.cancel_task(admitted.task_id, idempotency_key="cancel-cleared-owner")
            else:
                daemon.service.store.begin_runtime_session("recovery-readback")
            raise TimeoutError("publication acknowledgement unavailable")

        monkeypatch.setattr(host, "_run_task_impl", execute)
        monkeypatch.setattr(client, "settle", unavailable)
        monkeypatch.setattr(generic_host, "_LEASE_POLL_SECONDS", 60)
        with pytest.raises(TimeoutError, match="publication acknowledgement unavailable"):
            host.run(once=True)
        for owner in list(host._reconciliation_owners.values()):
            owner.join(timeout=5)
        assert not host._reconciliation_owners
        durable = reader.get_task(admitted.task_id)
        assert durable.state == ("cancelled" if disposition == "cancelled" else "queued")
        assert durable.attempt_id is None
        terminal = terminals[0]
        assert terminal["recorded"] and terminal["retain_spool"] and not terminal["settled"]
        assert terminal["status"] == ("cancelled" if disposition == "cancelled" else "failed")
        assert terminal["lease_diagnostic"]["category"] == (
            "publication_terminal" if disposition == "cancelled" else "publication_owner_changed")
        diagnostic = terminal["lease_diagnostic"]
        assert not {"runtime_code", "request_id", "exception_type"} & diagnostic.keys()
        receipt = json.loads((root / "result.json").read_text())
        assert receipt["status"] == terminal["status"]
        assert receipt["publication"]["status"] != "settled"
    finally:
        daemon.stop()
