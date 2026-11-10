from __future__ import annotations

import pytest

from astrid.core.execution.managed_tool_session import (
    CapabilityDescriptor,
    ManagedToolSession,
    SessionBinding,
    SessionCapacityError,
    StaleAdmissionError,
    UncertainCancellation,
)


class _Adapter:
    def __init__(self, *, fail_release: bool = False) -> None:
        self.events: list[tuple[str, str]] = []
        self.fail_release = fail_release

    def fence(self, *, reason: str) -> None:
        self.events.append(("fence", reason))

    def release(self, *, reason: str) -> dict[str, bool]:
        self.events.append(("release", reason))
        if self.fail_release:
            raise RuntimeError("release not observed")
        return {"ok": True, "released": True}


def _binding(suffix: str) -> SessionBinding:
    return SessionBinding(
        session_id=f"session-{suffix}",
        runtime_instance_id=f"runtime-{suffix}",
        process_birth_id=f"birth-{suffix}",
        endpoint=f"http://127.0.0.1:{8000 + len(suffix)}",
        source_digest=f"source-{suffix}",
        config_digest=f"config-{suffix}",
    )


def test_serial_admission_and_settlement_are_one_shot() -> None:
    manager = ManagedToolSession()
    adapter = _Adapter()
    envelope = manager.open(
        capability=CapabilityDescriptor("vibecomfy.run"),
        binding=_binding("a"),
        adapter=adapter,
    )
    token = manager.admit(capability_id="vibecomfy.run", invocation_id="task-a")
    assert envelope.schema_version == "astrid.managed-tool-session.v1"
    with pytest.raises(SessionCapacityError):
        manager.admit(capability_id="vibecomfy.run", invocation_id="task-b")
    evidence = {
        "cas": "sha256:output",
        "generation": token.generation,
        "binding_identity": list(token.binding_identity),
    }
    settled = manager.settle(token, result_evidence=evidence)
    assert settled.state == "settled"
    with pytest.raises(StaleAdmissionError):
        manager.settle(token, result_evidence=evidence)


def test_compatible_binding_reuses_owned_session_without_replacement() -> None:
    manager = ManagedToolSession()
    adapter = _Adapter()
    binding = _binding("a")
    first = manager.open(
        capability=CapabilityDescriptor("vibecomfy.run"),
        binding=binding,
        adapter=adapter,
    )
    reused = manager.open(
        capability=CapabilityDescriptor("vibecomfy.run"),
        binding=binding,
        adapter=_Adapter(),
    )

    assert first.state == "cold"
    assert reused.state == "warm"
    assert reused.generation == first.generation
    assert adapter.events == []


def test_incompatible_binding_fences_and_releases_before_replacement() -> None:
    manager = ManagedToolSession()
    old_adapter = _Adapter()
    manager.open(
        capability=CapabilityDescriptor("vibecomfy.run"),
        binding=_binding("a"),
        adapter=old_adapter,
    )
    old_token = manager.admit(capability_id="vibecomfy.run", invocation_id="task-a")
    new_adapter = _Adapter()
    replacement = manager.open(
        capability=CapabilityDescriptor("vibecomfy.run"),
        binding=_binding("b"),
        adapter=new_adapter,
    )
    assert replacement.binding.session_id == "session-b"
    assert old_adapter.events == [
        ("fence", "capacity_replacement"),
        ("release", "capacity_replacement"),
    ]
    with pytest.raises(StaleAdmissionError):
        manager.settle(
            old_token,
            result_evidence={
                "cas": "sha256:stale",
                "generation": old_token.generation,
                "binding_identity": list(old_token.binding_identity),
            },
        )


def test_execution_identity_change_replaces_same_process_session() -> None:
    manager = ManagedToolSession()
    first = _binding("a")
    second = SessionBinding(
        session_id=first.session_id,
        runtime_instance_id=first.runtime_instance_id,
        process_birth_id=first.process_birth_id,
        endpoint=first.endpoint,
        source_digest=first.source_digest,
        config_digest=first.config_digest,
        execution_identity="model-b-template-b",
    )
    old_adapter = _Adapter()
    manager.open(
        capability=CapabilityDescriptor("vibecomfy.run"),
        binding=first,
        adapter=old_adapter,
    )
    replacement = manager.open(
        capability=CapabilityDescriptor("vibecomfy.run"),
        binding=second,
        adapter=_Adapter(),
    )
    assert replacement.binding.execution_identity == "model-b-template-b"
    assert old_adapter.events == [
        ("fence", "capacity_replacement"),
        ("release", "capacity_replacement"),
    ]


def test_uncertain_cancellation_fences_the_session() -> None:
    manager = ManagedToolSession()
    adapter = _Adapter()
    manager.open(
        capability=CapabilityDescriptor("comfy_engine"),
        binding=_binding("a"),
        adapter=adapter,
    )
    token = manager.admit(capability_id="comfy_engine", invocation_id="task-a")
    with pytest.raises(UncertainCancellation):
        manager.cancel(token, outcome="uncertain")
    assert manager.generation == 2
    with pytest.raises(StaleAdmissionError):
        manager.settle(
            token,
            result_evidence={
                "cas": "sha256:late",
                "generation": token.generation,
                "binding_identity": list(token.binding_identity),
            },
        )


def test_confirmed_cancellation_requires_adapter_evidence() -> None:
    manager = ManagedToolSession()
    adapter = _Adapter()
    manager.open(
        capability=CapabilityDescriptor("comfy_engine"),
        binding=_binding("a"),
        adapter=adapter,
    )
    token = manager.admit(capability_id="comfy_engine", invocation_id="task-a")

    with pytest.raises(UncertainCancellation, match="not verified"):
        manager.cancel(token, outcome="confirmed")

    assert manager.active is False
    assert manager.occupied is True
    with pytest.raises(StaleAdmissionError):
        manager.settle(
            token,
            result_evidence={
                "cas": "sha256:late",
                "generation": token.generation,
                "binding_identity": list(token.binding_identity),
            },
        )


def test_release_failure_keeps_the_slot_poisoned() -> None:
    manager = ManagedToolSession()
    adapter = _Adapter(fail_release=True)
    manager.open(
        capability=CapabilityDescriptor("comfy_engine"),
        binding=_binding("a"),
        adapter=adapter,
    )
    with pytest.raises(SessionCapacityError):
        manager.open(
            capability=CapabilityDescriptor("other"),
            binding=_binding("b"),
            adapter=_Adapter(),
        )
    assert manager.active is False
    assert manager.occupied is True
    assert manager.generation == 2


def _settle_checked_token(manager, token):
    return manager.settle(token, result_evidence={
        "generation": token.generation, "binding_identity": list(token.binding_identity),
        "token_id": token.token_id, "invocation_id": token.invocation_id,
    })


@pytest.mark.parametrize("fault", ["none", "completed_intervening", "pending", "fenced", "binding", "adapter", "generation"])
def test_checked_quiescent_replacement_retains_admission_history(fault):
    manager = ManagedToolSession()
    old = _Adapter()
    binding = _binding("old")
    manager.open(capability=CapabilityDescriptor("vibecomfy.run"), binding=binding, adapter=old)
    original = manager.admit(capability_id="vibecomfy.run", invocation_id="original")
    _settle_checked_token(manager, original)
    expected = original
    if fault in {"completed_intervening", "pending"}:
        later = manager.admit(capability_id="vibecomfy.run", invocation_id="later")
        if fault == "completed_intervening":
            _settle_checked_token(manager, later)
            assert manager._active.in_flight == {} and manager._active.dispatched == set()
    elif fault == "fenced":
        manager.fence(reason="test-fence")
    elif fault == "binding":
        from dataclasses import replace
        expected = replace(original, binding_identity=_binding("different").identity_key)
    elif fault == "generation":
        from dataclasses import replace
        expected = replace(original, generation=original.generation + 1)
    replacement = _Adapter()
    kwargs = dict(expected_adapter=_Adapter() if fault == "adapter" else old,
                  expected_token=expected, capability=CapabilityDescriptor("cpu", resources_claimed=("cpu",)),
                  binding=_binding("cpu"), adapter=replacement, reason="checked-eviction")
    if fault != "none":
        before = list(old.events)
        with pytest.raises(StaleAdmissionError, match="authorization is stale"):
            manager._replace_quiescent(**kwargs)
        assert old.events == before and manager.current_adapter is old and manager.occupied
        return
    result = manager._replace_quiescent(**kwargs)
    assert result.state == "cold" and manager.current_adapter is replacement
    assert old.events == [("fence", "checked-eviction"), ("release", "checked-eviction")]
    with pytest.raises(StaleAdmissionError):
        _settle_checked_token(manager, original)


def test_checked_replacement_accepts_fresh_authorization_after_intervening_settlement():
    manager = ManagedToolSession()
    old = _Adapter()
    manager.open(capability=CapabilityDescriptor("vibecomfy.run"), binding=_binding("a"), adapter=old)
    first = manager.admit(capability_id="vibecomfy.run", invocation_id="first")
    _settle_checked_token(manager, first)
    latest = manager.admit(capability_id="vibecomfy.run", invocation_id="latest")
    _settle_checked_token(manager, latest)
    new = _Adapter()
    manager._replace_quiescent(expected_adapter=old, expected_token=latest,
        capability=CapabilityDescriptor("cpu", resources_claimed=("cpu",)),
        binding=_binding("cpu"), adapter=new, reason="checked-eviction")
    assert manager.current_adapter is new


def test_checked_replacement_release_failure_retains_poisoned_slot():
    manager = ManagedToolSession()
    old = _Adapter(fail_release=True)
    manager.open(capability=CapabilityDescriptor("vibecomfy.run"), binding=_binding("a"), adapter=old)
    token = manager.admit(capability_id="vibecomfy.run", invocation_id="task")
    _settle_checked_token(manager, token)
    with pytest.raises(SessionCapacityError):
        manager._replace_quiescent(expected_adapter=old, expected_token=token,
            capability=CapabilityDescriptor("cpu"), binding=_binding("cpu"), adapter=_Adapter(), reason="checked-eviction")
    assert manager.occupied and not manager.active and manager.current_adapter is old
