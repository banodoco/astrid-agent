from __future__ import annotations

import pytest

from astrid.core.execution.generic_host import HostError, RuntimeProtocolClient


class _Generated:
    def __init__(self) -> None:
        self.epochs = [3]
        self.heartbeats: list[dict[str, object]] = []

    def health(self):
        return {"runtime_epoch": self.epochs[-1]}

    def claim_task(self, **kwargs):
        return {
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "lease_id": "lease-1",
            "fence": 2,
            "runtime_epoch": kwargs["runtime_epoch"],
        }

    def heartbeat_attempt(self, attempt_id, **kwargs):
        self.heartbeats.append({"attempt_id": attempt_id, **kwargs})
        return {"ok": True}


def _client() -> RuntimeProtocolClient:
    client = RuntimeProtocolClient.__new__(RuntimeProtocolClient)
    client.generated = _Generated()
    client._runtime_epoch = None
    client._attempt_runtime_epochs = {}
    client._heartbeat_session = "session"
    client._heartbeat_sequence = 0
    import threading

    client._heartbeat_lock = threading.Lock()
    return client


def test_claim_epoch_is_reused_and_not_replaced_after_runtime_restart() -> None:
    client = _client()
    claim = client.claim_next(
        executor_id="worker-1",
        capability_ids=["render.basic"],
        idempotency_key="claim-1",
    )
    assert claim["runtime_epoch"] == 3

    client.generated.epochs.append(4)
    with pytest.raises(HostError, match="epoch changed after claim"):
        client.heartbeat(
            "task-1",
            "lease-1",
            attempt_id="attempt-1",
            fence=2,
        )
    assert client.generated.heartbeats == []
