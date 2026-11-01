"""Host contract tests; no Comfy server or provider is contacted."""
from dataclasses import replace

import pytest

from astrid.core.execution.generic_host import _ManagedVibeSessionAdapter
from astrid.core.execution.managed_tool_session import (
    CapabilityDescriptor, ManagedToolSession, SessionBinding, SessionCapacityError,
    StaleAdmissionError, UncertainCancellation,
)


def binding():
    return SessionBinding("session", "runtime", "process-birth", "http://example.invalid",
                          "source", "config", "residency", 3, "launch", "engine-birth")


class FakeComfy:
    """A single owner with independent mutable evidence and prompt custody."""
    def __init__(self, bound, *, attached=False):
        self.bound = bound
        self.evidence = bound
        self.attached = attached
        self.pid = 1234
        self.active = None
        self.calls = []
        self.uncertain = False
        self.fenced = False

    def observe(self, *, binding):
        return {"ok": self.evidence is not None and binding == self.bound == self.evidence}

    def invoke(self, *, token, model_id, template_id):
        assert not self.fenced and token.binding_identity == self.bound.identity_key
        self.active = token
        self.calls.append(("invoke", token, model_id, template_id))
        return {"ok": True, "child_context": {"token_id": token.token_id,
                "invocation_id": token.invocation_id, "generation": token.generation,
                "binding_identity": list(token.binding_identity)}}

    def fence(self, *, reason):
        self.fenced = True
        self.calls.append(("fence", self.active, reason))
        return {"ok": True, "fenced": True}

    def cancel(self, *, reason):
        self.fenced = True
        self.calls.append(("cancel_prompt", self.active, reason))
        if self.uncertain:
            return {"ok": False, "cancelled": False}
        self.active = None
        return {"ok": True, "cancelled": True}

    def release(self, *, reason):
        self.calls.append(("quiesce", self.active, reason))
        if self.uncertain:
            return {"ok": False, "released": False}
        if self.active is not None:
            self.calls.append(("cancel_prompt", self.active, reason))
        self.active = None
        if self.attached:
            return {"ok": True, "released": True, "external_residency_unchanged": True,
                    "release_requested": False, "memory_observed_returned": False}
        self.calls.append(("free_owned", self.bound))
        return {"ok": True, "released": True, "release_requested": True,
                "memory_observed_returned": False}


def manager_with(fake):
    manager = ManagedToolSession()
    adapter = _ManagedVibeSessionAdapter(fake)
    manager.open(capability=CapabilityDescriptor("vibecomfy.run"), binding=fake.bound, adapter=adapter)
    return manager, adapter


def begin(manager, model="model", template="template", invocation="task"):
    token = manager.admit(capability_id="vibecomfy.run", invocation_id=invocation)
    adapter = manager.adapter_for(token, begin=True)
    adapter.invoke(token, model_id=model, template_id=template)
    return token


def settle(manager, token):
    manager.settle(token, result_evidence={"token_id": token.token_id, "invocation_id": token.invocation_id,
                    "generation": token.generation, "binding_identity": list(token.binding_identity)})


def test_open_passes_complete_binding_to_owner_hook(monkeypatch):
    from astrid.core.generation.backends.vibecomfy import CheckoutServerAdapter
    expected = binding()
    fake = FakeComfy(expected)
    seen = []
    def open_host_session(cls, *, binding, hc03_profile):
        seen.append((binding, hc03_profile))
        return fake
    monkeypatch.setattr(CheckoutServerAdapter, "open_host_session", classmethod(open_host_session), raising=False)
    bridge = _ManagedVibeSessionAdapter.from_host_session(binding=expected, hc03_profile={"owner": "verified"})
    assert seen == [(expected, {"owner": "verified"})]
    assert bridge.observe(binding=expected)["ok"]


def test_warm_reuse_refreshes_context_and_keeps_pid():
    fake = FakeComfy(binding())
    manager, bridge = manager_with(fake)
    first = begin(manager, template="a", invocation="task-a")
    settle(manager, first)
    pid = fake.pid
    manager.open(capability=CapabilityDescriptor("vibecomfy.run"), binding=fake.bound,
                 adapter=_ManagedVibeSessionAdapter(FakeComfy(fake.bound)))
    second = begin(manager, template="b", invocation="task-b")
    assert manager.current_adapter is bridge and fake.pid == pid
    assert fake.calls[:2] == [("invoke", first, "model", "a"), ("invoke", second, "model", "b")]
    settle(manager, second)
    manager.release()


@pytest.mark.parametrize("field", list(SessionBinding.__dataclass_fields__) + ["missing"])
def test_observe_refuses_every_changed_or_missing_binding_field(field):
    fake = FakeComfy(binding())
    manager, _ = manager_with(fake)
    fake.evidence = None if field == "missing" else replace(fake.bound, **{
        field: 4 if field == "runtime_epoch" else "changed"})
    with pytest.raises(StaleAdmissionError):
        manager.observe(fake.bound)
    assert manager.occupied and not manager.active


def test_attached_cancel_is_prompt_specific_and_pre_dispatch_never_cancels_old_prompt():
    fake = FakeComfy(binding(), attached=True)
    manager, _ = manager_with(fake)
    first = begin(manager, invocation="first")
    manager.cancel(first, outcome="confirmed")
    second = manager.admit(capability_id="vibecomfy.run", invocation_id="second")
    manager.cancel(second, outcome="confirmed")
    assert [event[:2] for event in fake.calls if event[0] == "cancel_prompt"] == [("cancel_prompt", first)]
    result = manager.release()
    assert result["native"]["external_residency_unchanged"] is True
    assert all(event[0] != "free_owned" for event in fake.calls)


def test_uncertain_cancel_keeps_slot_fenced_until_quiescence_is_proven():
    fake = FakeComfy(binding(), attached=True)
    manager, _ = manager_with(fake)
    token = begin(manager)
    fake.uncertain = True
    with pytest.raises(UncertainCancellation):
        manager.cancel(token, outcome="confirmed")
    assert manager.occupied and not manager.active
    with pytest.raises(SessionCapacityError):
        manager.release()
    assert manager.occupied and not manager.active
    fake.uncertain = False
    manager.release()
    assert not manager.occupied


def test_release_quiesces_exact_owned_work_and_preserves_process():
    fake = FakeComfy(binding())
    manager, _ = manager_with(fake)
    token = begin(manager)
    with pytest.raises(SessionCapacityError):
        manager.release(expected_binding=fake.bound, expected_generation=manager.generation, require_quiescent=True)
    assert fake.calls == [("invoke", token, "model", "template")]
    settle(manager, token)
    pid = fake.pid
    result = manager.release()
    assert ("quiesce", token, "released") in fake.calls
    assert ("free_owned", fake.bound) in fake.calls and fake.pid == pid
    assert result["native"]["release_requested"] and not result["native"]["memory_observed_returned"]


def test_invocation_refuses_mismatched_child_context():
    fake = FakeComfy(binding())
    manager, adapter = manager_with(fake)
    token = manager.admit(capability_id="vibecomfy.run", invocation_id="task")
    manager.adapter_for(token, begin=True)
    fake.invoke = lambda **kwargs: {"ok": True, "child_context": {
        "token_id": "stale", "invocation_id": token.invocation_id,
        "generation": token.generation, "binding_identity": list(token.binding_identity)}}
    with pytest.raises(Exception, match="child context does not match"):
        adapter.invoke(token, model_id="model", template_id="template")
    manager.fence_admission(token, reason="invocation_failed")
    assert manager.occupied and not manager.active
