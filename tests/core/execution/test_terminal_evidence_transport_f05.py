from __future__ import annotations

from astrid.core.execution.generic_host import RuntimeProtocolClient


class Generated:
    timeout = 30.0

    def __init__(self, result):
        self.result = result
        self.calls = []
        self.task_result = None

    def fail_attempt(self, attempt_id, **kwargs):
        self.calls.append(("fail", attempt_id, kwargs))
        return {"task": {"result": self.result}}

    def get_task(self, task_id):
        return {"id": task_id, "attempt_id": "attempt-1", "version": 4,
                "result": self.task_result}

    def cancel_task(self, task_id, **kwargs):
        self.calls.append(("cancel", task_id, kwargs))
        return {"task": {"result": self.result}}


def _client(result):
    client = RuntimeProtocolClient.__new__(RuntimeProtocolClient)
    client.generated = Generated(result)
    client._attempt_runtime_epochs = {"attempt-1": 9}
    return client


def test_failure_forwards_outputs_and_flat_result_and_checks_readback():
    result = {"status": "failed", "manifest_ref": {"object_id": "sha256:" + "a" * 64, "size": 12}}
    client = _client(result)
    outputs = [{"name": "result_manifest", "output_port": "result_manifest", "role": "manifest"}]

    client.fail("task-1", "lease-1", "producer failed", attempt_id="attempt-1", fence=3,
                outputs=outputs, result=result)

    method, attempt_id, payload = client.generated.calls[0]
    assert method == "fail"
    assert attempt_id == "attempt-1"
    assert payload["outputs"] == outputs
    assert payload["result"] == result
    assert "outputs" not in payload["result"]


def test_cancel_evidence_carries_complete_fence_and_unknown_stop():
    result = {"status": "cancelled", "manifest_ref": {"object_id": "sha256:" + "b" * 64, "size": 20}}
    client = _client(result)
    outputs = [{"name": "result_manifest", "output_port": "result_manifest", "role": "manifest"}]

    client.cancel("task-1", attempt_id="attempt-1", fence=3, lease_id="lease-1",
                  outputs=outputs, result=result, stop_observed=False)

    method, task_id, payload = client.generated.calls[0]
    assert method == "cancel"
    assert task_id == "task-1"
    assert payload["attempt_id"] == "attempt-1"
    assert payload["lease_id"] == "lease-1"
    assert payload["fence"] == 3
    assert payload["runtime_epoch"] == 9
    assert payload["outputs"] == outputs
    assert payload["result"] == result
    assert payload["stop_observed"] is False
    pending_key = payload["idempotency_key"]

    client.cancel("task-1", attempt_id="attempt-1", fence=3, lease_id="lease-1",
                  outputs=outputs, result=result, stop_observed=True)
    observed_key = client.generated.calls[1][2]["idempotency_key"]
    assert observed_key != pending_key


def test_failure_lost_ack_uses_durable_readback_without_replaying():
    result = {"status": "failed", "manifest_ref": {"object_id": "sha256:" + "c" * 64, "size": 8}}
    client = _client({"status": "failed"})

    def commit_then_lose_ack(attempt_id, **kwargs):
        client.generated.calls.append(("fail", attempt_id, kwargs))
        client.generated.task_result = result
        raise OSError("response lost after commit")

    client.generated.fail_attempt = commit_then_lose_ack
    response = client.fail("task-1", "lease-1", "producer failed", attempt_id="attempt-1",
                           fence=3, result=result)

    assert len(client.generated.calls) == 1
    assert response["task"]["result"] == result


def test_failure_retries_same_idempotent_write_if_readback_is_not_yet_visible():
    result = {"status": "failed", "manifest_ref": {"object_id": "sha256:" + "d" * 64, "size": 9}}
    client = _client({"status": "failed"})

    def delayed_visibility(attempt_id, **kwargs):
        client.generated.calls.append(("fail", attempt_id, kwargs))
        if len(client.generated.calls) == 2:
            client.generated.task_result = result
            return {"task": {"result": result}}
        raise OSError("response lost before readback visibility")

    client.generated.fail_attempt = delayed_visibility
    client.fail("task-1", "lease-1", "producer failed", attempt_id="attempt-1",
                fence=3, result=result)

    assert len(client.generated.calls) == 2
    assert client.generated.calls[0][2]["idempotency_key"] == client.generated.calls[1][2]["idempotency_key"]
