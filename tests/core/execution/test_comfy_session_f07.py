"""CPU-only acceptance of the existing host's single Comfy session slot."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.execution.managed_tool_session import (
    CapabilityDescriptor,
    ManagedToolSession,
    SessionCapacityError,
    StaleAdmissionError,
)
from astrid.core.execution.warm_hold import WarmHolds, idle_seconds
from tests.core.execution.test_managed_tool_session import _Adapter, _binding, _settle_checked_token
from tests.core.execution.test_warm_hold import Timers
from tests.test_generic_host import FakeRuntime
from tests.test_generic_host_vibecomfy_lifecycle import _FakeCheckout, _install_fixture, _profile, _task


@pytest.mark.parametrize("fault", ["false", "raise"])
def test_failed_fence_retains_slot_without_releasing_or_allocating(fault):
    manager, old = ManagedToolSession(), _Adapter()

    def fence(*, reason):
        old.events.append(("fence", reason))
        if fault == "raise":
            raise RuntimeError("uncertain native fence")
        return {"ok": False}

    old.fence = fence
    manager.open(capability=CapabilityDescriptor("vibecomfy.run"), binding=_binding("a"), adapter=old)
    token = manager.admit(capability_id="vibecomfy.run", invocation_id="a")
    allocated = []
    with pytest.raises(SessionCapacityError):
        manager.open(capability=CapabilityDescriptor("vibecomfy.run"), binding=_binding("b"),
                     adapter=_Adapter(), start=lambda: allocated.append(True))
    assert manager.occupied and not manager.active and manager.current_adapter is old
    assert allocated == [] and old.events == [("fence", "capacity_replacement")]
    with pytest.raises(StaleAdmissionError):
        _settle_checked_token(manager, token)
    old.fence = lambda **kwargs: {"ok": True}
    manager.release(reason="reconciled")
    assert not manager.occupied


def test_observation_exception_fences_late_results_and_preserves_custody():
    manager, adapter = ManagedToolSession(), _Adapter()
    binding = _binding("a")
    manager.open(capability=CapabilityDescriptor("vibecomfy.run"), binding=binding, adapter=adapter)
    token = manager.admit(capability_id="vibecomfy.run", invocation_id="a")
    adapter.observe = lambda **kwargs: (_ for _ in ()).throw(OSError("registry unavailable"))
    with pytest.raises(StaleAdmissionError, match="observation was not verified"):
        manager.observe(binding)
    assert manager.occupied and not manager.active
    with pytest.raises(StaleAdmissionError):
        _settle_checked_token(manager, token)
    assert adapter.events == [("fence", "observation_failed")]


@pytest.mark.parametrize("stale", ["binding", "generation", "fenced", "absent"])
def test_owner_release_refuses_stale_authorization_without_native_control(stale):
    manager, adapter = ManagedToolSession(), _Adapter()
    binding = _binding("a")
    opened = manager.open(capability=CapabilityDescriptor("vibecomfy.run"), binding=binding, adapter=adapter)
    expected_binding, expected_generation = binding, opened.generation
    if stale == "binding":
        expected_binding = replace(binding, engine_birth_id="another-process")
    elif stale == "generation":
        expected_generation += 1
    elif stale == "fenced":
        manager.fence(reason="uncertain")
    else:
        manager.release()
    before = list(adapter.events)
    with pytest.raises(StaleAdmissionError):
        manager.release(expected_binding=expected_binding, expected_generation=expected_generation,
                        require_quiescent=True)
    assert adapter.events == before


def test_release_waits_for_exact_admission_then_preserves_native_evidence():
    manager, adapter = ManagedToolSession(), _Adapter()
    binding = _binding("a")
    opened = manager.open(capability=CapabilityDescriptor("vibecomfy.run"), binding=binding, adapter=adapter)
    token = manager.admit(capability_id="vibecomfy.run", invocation_id="a")
    manager.adapter_for(token, begin=True)
    kwargs = dict(expected_binding=binding, expected_generation=opened.generation, require_quiescent=True)
    with pytest.raises(SessionCapacityError, match="pending active admissions"):
        manager.release(**kwargs)
    assert manager.active and adapter.events == []
    _settle_checked_token(manager, token)
    evidence = manager.release(**kwargs)
    assert evidence == {"ok": True, "released": True} and not manager.occupied


def _comfy_host(tmp_path, monkeypatch, *, spawn_child=False):
    from astrid.core.generation.backends import vibecomfy

    digest, data = _install_fixture(tmp_path)
    runtime = FakeRuntime()
    runtime.get_object = lambda requested: data
    monkeypatch.setattr(generic_host, "_read_readiness_profile_document", _profile)
    monkeypatch.setattr(generic_host, "_prepare_vibecomfy_execution_identity", lambda *args: (
        "execution-a", "model-a", "template-a", {"model_id": "model-a", "resident_metadata": {"model_assets": ["model-a"]}}))
    events, backends = [], []

    def construct(cls, **kwargs):
        backend = _FakeCheckout(events, spawn_child=spawn_child and not backends, **kwargs)
        backends.append(backend)
        return backend

    monkeypatch.setattr(vibecomfy.CheckoutServerAdapter, "from_host_session", classmethod(construct))
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    host.warm_holds.close()
    timers = Timers()
    host.warm_holds = WarmHolds(timer=timers)

    def run(name):
        task = _task(name, digest)
        runtime.tasks[name] = task
        return host.run_task(task, lease_token=f"lease-{name}")

    return host, runtime, timers, events, backends, run


def test_same_pid_reuse_and_stale_timer_after_owner_release(tmp_path, monkeypatch):
    host, runtime, timers, events, backends, run = _comfy_host(tmp_path, monkeypatch, spawn_child=True)
    try:
        run("first")
        backend = host.managed_tool_session.current_adapter.backend
        pid = backend.child.pid
        first_timer = timers.live()
        run("second")
        assert host.managed_tool_session.current_adapter.backend is backend
        assert backend.child.pid == pid and backend.child.poll() is None
        assert first_timer.cancelled
        first_timer.callback()
        assert "release:idle" not in events
        envelope = runtime.settlements[-1][2]["result"]["managed_tool_session"]
        assert envelope["binding"]["process_birth_id"] == "process-a"
        manager = host.managed_tool_session
        stale_timer = timers.live()
        result = host.release_managed_session(expected_binding=manager.current_binding,
                                              expected_generation=manager.generation)
        assert result["ok"] and result["native"]["released"]
        assert timers.live() is None and host.warm_holds.status() == []
        run("third")
        replacement = manager.current_adapter
        stale_timer.callback()
        assert manager.current_adapter is replacement and manager.active
        assert "release:idle" not in events
        assert [item[0] for item in runtime.settlements] == ["first", "second", "third"]
    finally:
        host.warm_holds.close()
        host.managed_tool_session.close()
        for backend in backends:
            if backend.child is not None and backend.child.poll() is None:
                backend.child.terminate()
                backend.child.wait(timeout=5)


def test_host_serializes_comfy_setup_and_release_cannot_race_it(tmp_path, monkeypatch):
    host, runtime, timers, events, backends, run = _comfy_host(tmp_path, monkeypatch)
    entered, proceed, contended = threading.Event(), threading.Event(), threading.Event()
    real_lock = threading.Lock()

    class ObservedLock:
        def acquire(self, *, blocking=True, timeout=-1):
            if blocking and real_lock.locked():
                contended.set()
            return real_lock.acquire(blocking=blocking, timeout=timeout)

        def release(self):
            real_lock.release()

    host._session_task_lock = ObservedLock()
    materialize = host._materialize_inputs
    calls = []

    def block_first(*args, **kwargs):
        calls.append(threading.get_ident())
        if len(calls) == 1:
            entered.set()
            assert proceed.wait(5)
        return materialize(*args, **kwargs)

    monkeypatch.setattr(host, "_materialize_inputs", block_first)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run, "first")
            assert entered.wait(5)
            second = pool.submit(run, "second")
            try:
                assert contended.wait(5)
                assert len(calls) == 1 and not second.done()
                result = host.release_managed_session(expected_binding=_binding("irrelevant"), expected_generation=0)
                assert result == {"ok": False, "released": False, "release_pending": True}
                assert "cancel" not in events
            finally:
                proceed.set()
            assert first.result(timeout=10)["task"]["status"] == "completed"
            assert second.result(timeout=10)["task"]["status"] == "completed"
        assert len(calls) == 2
        assert [row[0] for row in runtime.settlements] == ["first", "second"]
        assert len(timers.armed) == 2 and timers.armed[0].cancelled
    finally:
        proceed.set()
        host.warm_holds.close()
        host.managed_tool_session.close()


def test_failed_owner_release_retains_poisoned_comfy_slot(tmp_path, monkeypatch):
    host, runtime, timers, events, backends, run = _comfy_host(tmp_path, monkeypatch)
    notes = []
    monkeypatch.setattr(host, "_latch_cleanup_uncertainty", notes.append)
    try:
        run("first")
        manager = host.managed_tool_session
        original = manager.current_adapter
        monkeypatch.setattr(original.backend, "release", lambda **kwargs: {"ok": False, "released": False})
        result = host.release_managed_session(expected_binding=manager.current_binding,
                                              expected_generation=manager.generation)
        assert result["release_pending"] and not result["released"]
        assert manager.current_adapter is original and manager.occupied and not manager.active
        assert len(notes) == 1
        with pytest.raises(SessionCapacityError):
            run("second")
        assert manager.current_adapter is original
        assert [row[0] for row in runtime.settlements] == ["first"]
    finally:
        host.warm_holds.close()
        backends[0].release = lambda **kwargs: {"ok": True, "released": True}
        host.managed_tool_session.close()


@pytest.mark.parametrize("requested", [-1, float("nan"), float("inf"), True])
def test_invalid_idle_request_never_arms_a_timer(requested):
    with pytest.raises(ValueError, match="finite and non-negative"):
        idle_seconds(600, requested=requested, environ={"ASTRID_WARM_IDLE_SECONDS": "120"})


def test_zero_idle_request_overrides_environment_and_expires(tmp_path, monkeypatch):
    host, runtime, timers, events, backends, run = _comfy_host(tmp_path, monkeypatch)
    try:
        monkeypatch.setenv("ASTRID_WARM_IDLE_SECONDS", "120")
        run("first")
        assert timers.live().seconds == 120
        # Same path used after the admitted request's lifecycle is parsed.
        host._requested_idle_seconds = 0
        host._settle_session_hold(host._hold_session_slot())
        assert timers.live().seconds == 0
        timers.fire()
        assert not host.managed_tool_session.occupied and events[-1] == "release:idle"
    finally:
        host.warm_holds.close()
        host.managed_tool_session.close()
