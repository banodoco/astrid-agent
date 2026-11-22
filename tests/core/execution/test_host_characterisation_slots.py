"""HM2 R11/H16: a public multi-slot run shares accounting, not attempt owners."""

from __future__ import annotations

from tests.core.execution import test_generic_host_claim_loop as claim_cases

world = claim_cases.world


def test_r11_parallel_run_keeps_coordinator_resources_shared_and_slot_state_private(world):
    """R11/H16: retained slot state and combined quiescence survive the public run."""
    backend = claim_cases.Backend(2)
    host = claim_cases.host_for(
        world, backend, capacity=2, attempt_base=world.parent / "attempts",
    )
    ready = host.preflight()
    assert len(ready) == 1 and all(record.ready for record in ready)
    host.runtime_state = {"runtime_epoch": 1, "runtime_session_id": "hm2-runtime"}
    thread, outcome = claim_cases.run_thread(host, max_tasks=2)
    try:
        backend.wait(lambda: backend.active == 2)
        slots = tuple(row["host"] for row in backend.started)
        assert len(slots) == 2
        for slot in slots:
            assert slot._handoff_accounting_owner is host
            assert slot.execution_policy is host.execution_policy
            assert slot._evidence_observation_lock is host._evidence_observation_lock
            assert slot._unresolved_task_scopes is host._unresolved_task_scopes
            assert slot._local_engine_idle is host._local_engine_idle
            assert slot.runtime_state == host.runtime_state
            assert slot.runtime_state is not host.runtime_state
            assert slot.capabilities == host.capabilities
            assert slot.capabilities is not host.capabilities
        for attribute in (
            "managed_tool_session", "_session_task_lock", "_process_lock",
            "_active_processes", "_child_bridges", "_host_lifetime_processes",
            "warm_holds", "_shutdown", "_provider_grants",
        ):
            assert len({id(getattr(owner, attribute)) for owner in (host, *slots)}) == 3, attribute
        assert slots[0].runtime_state is not slots[1].runtime_state
        assert slots[0].capabilities is not slots[1].capabilities
        snapshot = host.handoff_quiescence()
        assert snapshot["active_attempts"] == 2
        assert snapshot["claim_rpc_in_flight"] == snapshot["pending_settlements"] == 0
        disposition, measured = host.pause_claim_admission()
        assert disposition == "active_work"
        assert measured["active_attempts"] == 2
        assert measured["claim_gate_closed"] is False

        backend.release.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert "error" not in outcome
        assert sorted(result["task_id"] for result in outcome["result"]) == ["0", "1"]
        assert host.handoff_quiescence()["active_attempts"] == 0
        disposition, measured = host.pause_claim_admission()
        assert disposition == "paused" and measured["claim_gate_closed"] is True
        restored, measured = host.restore_claim_admission()
        assert restored is True and measured["claim_gate_closed"] is False
        assert host._claim_slots == ()
    finally:
        backend.release.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        host.shutdown()
        thread.join(timeout=5)
        assert not thread.is_alive()
