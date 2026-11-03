"""Legacy Comfy lifecycle operations cannot stand in for release authority."""
import pytest

from astrid.core.execution.generic_host import _ManagedVibeSessionAdapter
from astrid.core.execution.managed_tool_session import (
    CapabilityDescriptor, ManagedToolSession, SessionBinding, SessionCapacityError,
)


class Backend:
    def __init__(self):
        self.calls = []

    def fence(self, **kwargs):
        self.calls.append("fence")
        return {"ok": True, "fenced": True}

    def release(self, **kwargs):
        self.calls.append("release")
        return {"ok": True, "released": True}


def binding(name):
    return SessionBinding(name, "owner", "birth", "http://127.0.0.1:8188", "source", "config")


@pytest.mark.parametrize("operation", ["requested", "idle", "task_settled", "host_shutdown", "replacement", "restart"])
def test_legacy_release_refuses_before_native_fence_and_retains_slot(operation):
    backend = Backend()
    adapter = _ManagedVibeSessionAdapter(backend)
    manager = ManagedToolSession()
    first = binding("first")
    opened = manager.open(capability=CapabilityDescriptor("vibecomfy.run"), binding=first, adapter=adapter)
    token = manager.admit(capability_id="vibecomfy.run", invocation_id="running")
    with pytest.raises(SessionCapacityError, match="authority unavailable"):
        if operation == "replacement":
            manager.open(capability=CapabilityDescriptor("vibecomfy.run"), binding=binding("next"), adapter=Backend())
        elif operation == "restart":
            manager.restart(binding=binding("next"), adapter=Backend())
        elif operation == "host_shutdown":
            manager.close()
        else:
            manager.release(reason=operation)
    assert backend.calls == []
    assert manager.occupied and manager.active and manager.current_adapter is adapter
    assert manager.current_binding == first and manager.generation == opened.generation
    manager.settle(token, result_evidence={"generation": token.generation, "binding_identity": token.binding_identity})


def test_direct_legacy_adapter_release_refuses():
    backend = Backend()
    with pytest.raises(SessionCapacityError, match="authority unavailable"):
        _ManagedVibeSessionAdapter(backend).release(reason="requested")
    assert backend.calls == []
