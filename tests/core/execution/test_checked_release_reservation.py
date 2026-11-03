import time

import pytest

from astrid.core.execution.managed_tool_session import (
    CapabilityDescriptor, ManagedToolSession, SessionBinding, SessionCapacityError,
    StaleAdmissionError,
)


class Adapter:
    def __init__(self):
        self.events = []

    def fence(self, *, reason):
        self.events.append(("fence", reason))

    def release(self, *, reason):
        self.events.append(("release", reason))
        return {"ok": True}


def setup_slot():
    manager, adapter = ManagedToolSession(), Adapter()
    binding = SessionBinding("session", "owner", "birth", "endpoint", "source", "config")
    opened = manager.open(capability=CapabilityDescriptor("engine"), binding=binding, adapter=adapter)
    return manager, adapter, binding, opened.generation


def reserve(manager, binding, generation):
    return manager._begin_checked_release(
        association_id="association", request_id="request", expected_binding=binding,
        expected_generation=generation, deadline=time.monotonic() + 10,
    )


def test_busy_preserves_generation_and_completed_never_calls_adapter():
    manager, adapter, binding, generation = setup_slot()
    intent = reserve(manager, binding, generation)
    assert intent.generation == generation
    with pytest.raises(StaleAdmissionError):
        manager.admit(capability_id="engine", invocation_id="task")
    with pytest.raises(SessionCapacityError):
        manager.release()
    manager._finish_checked_release(intent, outcome="busy")
    assert manager.active and manager.generation == generation
    intent = reserve(manager, binding, generation)
    manager._finish_checked_release(intent, outcome="completed")
    assert not manager.occupied and manager.generation == generation + 1
    assert adapter.events == []
    with pytest.raises(StaleAdmissionError):
        manager._reconcile_checked_release(intent, outcome="completed")


@pytest.mark.parametrize("outcome", ["failed", "unresolved"])
def test_uncertainty_retains_original_slot_and_blocks_replacement(outcome):
    manager, adapter, binding, generation = setup_slot()
    intent = reserve(manager, binding, generation)
    manager._finish_checked_release(intent, outcome=outcome)
    assert manager.occupied and not manager.active and manager.generation == generation + 1
    with pytest.raises(SessionCapacityError):
        manager.open(capability=CapabilityDescriptor("engine"), binding=binding, adapter=Adapter())
    with pytest.raises(SessionCapacityError):
        manager.close()
    if outcome == "unresolved":
        manager._reconcile_checked_release(intent, outcome="completed")
        assert not manager.occupied and manager.generation == generation + 1
    else:
        with pytest.raises(StaleAdmissionError):
            manager._reconcile_checked_release(intent, outcome="completed")
        assert manager.occupied
    assert adapter.events == []


def test_stale_and_nonquiescent_requests_do_not_fence_slot():
    manager, adapter, binding, generation = setup_slot()
    token = manager.admit(capability_id="engine", invocation_id="task")
    with pytest.raises(SessionCapacityError):
        reserve(manager, binding, generation)
    with pytest.raises(StaleAdmissionError):
        reserve(manager, binding, generation + 1)
    assert manager.active and manager.generation == generation and adapter.events == []
    manager.settle(token, result_evidence={"generation": generation,
                                           "binding_identity": binding.identity_key})
    reserve(manager, binding, generation)


def test_external_fence_prevents_old_readback_from_retiring_slot():
    manager, adapter, binding, generation = setup_slot()
    intent = reserve(manager, binding, generation)
    manager._finish_checked_release(intent, outcome="unresolved")
    manager.fence(reason="external")
    with pytest.raises(StaleAdmissionError):
        manager._reconcile_checked_release(intent, outcome="completed")
    assert manager.occupied and manager.generation == generation + 2
    assert adapter.events == [("fence", "external")]


def test_late_completion_requires_explicit_reconciliation(monkeypatch):
    manager, adapter, binding, generation = setup_slot()
    intent = reserve(manager, binding, generation)
    monkeypatch.setattr(time, "monotonic", lambda: intent.deadline + 1)
    manager._finish_checked_release(intent, outcome="completed")
    assert manager.occupied and not manager.active
    with pytest.raises(StaleAdmissionError):
        manager._finish_checked_release(intent, outcome="completed")
    manager._reconcile_checked_release(intent, outcome="completed")
    assert not manager.occupied and adapter.events == []
