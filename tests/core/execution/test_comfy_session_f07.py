"""CPU-only acceptance of the existing host's single Comfy session slot."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost, HostCancelled
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


@pytest.mark.parametrize("stale", ["binding", "generation", "boolean", "float", "fenced", "absent"])
def test_owner_release_refuses_stale_authorization_without_native_control(stale):
    manager, adapter = ManagedToolSession(), _Adapter()
    binding = _binding("a")
    opened = manager.open(capability=CapabilityDescriptor("vibecomfy.run"), binding=binding, adapter=adapter)
    expected_binding, expected_generation = binding, opened.generation
    if stale == "binding":
        expected_binding = replace(binding, engine_birth_id="another-process")
    elif stale == "generation":
        expected_generation += 1
    elif stale == "boolean":
        expected_generation = True
    elif stale == "float":
        expected_generation = float(opened.generation)
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


@pytest.mark.parametrize("binding,generation", [(None, None), (_binding("a"), True), (_binding("a"), 1.0)])
def test_host_release_never_falls_back_to_unchecked_owner_control(tmp_path, binding, generation):
    host, adapter = GenericPackHost(pack_roots=[tmp_path]), _Adapter()
    manager = host.managed_tool_session
    manager.open(capability=CapabilityDescriptor("vibecomfy.run"), binding=_binding("a"), adapter=adapter)
    try:
        with pytest.raises(StaleAdmissionError):
            host.release_managed_session(expected_binding=binding, expected_generation=generation)
        assert manager.active and adapter.events == []
    finally:
        host.warm_holds.close()
        manager.close()


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
        # Model release preserves the owned server. This CPU process stands
        # in for that server; fixture teardown alone terminates it.
        def release(*, reason="requested"):
            events.append(f"release:{reason}")
            return {"ok": True, "released": True, "memory_observed_returned": False}

        backend.release = release
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
        assert result["native"]["native"]["memory_observed_returned"] is False
        assert backend.child.pid == pid and backend.child.poll() is None
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


@pytest.mark.parametrize("trigger", ["owner", "idle"])
@pytest.mark.parametrize("failure", ["false", "raise"])
def test_failed_release_retains_poisoned_comfy_slot(tmp_path, monkeypatch, trigger, failure):
    host, runtime, timers, events, backends, run = _comfy_host(tmp_path, monkeypatch)
    notes = []
    monkeypatch.setattr(host, "_latch_cleanup_uncertainty", notes.append)
    try:
        run("first")
        manager = host.managed_tool_session
        original = manager.current_adapter
        def release(**kwargs):
            if failure == "raise":
                raise OSError("unload response lost")
            return {"ok": False, "released": False}

        monkeypatch.setattr(original.backend, "release", release)
        if trigger == "owner":
            result = host.release_managed_session(expected_binding=manager.current_binding,
                                                  expected_generation=manager.generation)
            assert result["release_pending"] and not result["released"]
        else:
            timers.fire()
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


def test_cancelled_session_wait_never_fences_the_running_comfy_task(tmp_path, monkeypatch):
    host, runtime, timers, events, backends, run = _comfy_host(tmp_path, monkeypatch)
    entered, proceed, cancelled = threading.Event(), threading.Event(), threading.Event()
    command = host._run_command_definition
    controls = []
    monkeypatch.setattr(generic_host, "_LEASE_POLL_SECONDS", 0.01)

    def cancel(task_id, **kwargs):
        controls.append((task_id, kwargs))
        cancelled.set()
        return {"task": {"status": "cancelled"}}

    def task(task_id):
        current = runtime.tasks[task_id]
        if task_id == "waiting":
            return {"task": {**current["task"], "status": "cancel_requested"}}
        return current

    def block_running(*args, **kwargs):
        entered.set()
        assert proceed.wait(5)
        return command(*args, **kwargs)

    monkeypatch.setattr(runtime, "cancel", cancel, raising=False)
    monkeypatch.setattr(runtime, "task", task)
    monkeypatch.setattr(host, "_run_command_definition", block_running)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(run, "running")
            assert entered.wait(5)
            manager = host.managed_tool_session
            (token,) = manager._active.in_flight.values()
            waiting = pool.submit(run, "waiting")
            try:
                assert cancelled.wait(5), "the waiting task's lease observer must keep running"
                with pytest.raises(HostCancelled):
                    waiting.result(timeout=5)
                assert controls[0][0] == "waiting" and controls[0][1]["stop_observed"] is True
                assert manager.active and manager.adapter_for(token) is not None
                assert "cancel" not in events
            finally:
                proceed.set()
            assert running.result(timeout=10)["task"]["status"] == "completed"
        assert [row[0] for row in runtime.settlements] == ["running"]
    finally:
        proceed.set()
        host.warm_holds.close()
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


def test_default_hold_reuses_engine_then_unloads_and_reloads(tmp_path, monkeypatch):
    """CPU stand-in tracks model residency; the host owns the real lease lifecycle."""
    monkeypatch.delenv("ASTRID_WARM_IDLE_SECONDS", raising=False)
    starts = []
    construct = _FakeCheckout.__init__

    def observed_construct(self, *args, **kwargs):
        if kwargs["spawn_child"]:
            starts.append(1)
        construct(self, *args, **kwargs)

    monkeypatch.setattr(_FakeCheckout, "__init__", observed_construct)
    host, runtime, timers, events, backends, run = _comfy_host(tmp_path, monkeypatch, spawn_child=True)
    now = [0.0]
    host.warm_holds._clock = lambda: now[0]
    model = {"loaded": False, "loads": 0, "unloads": 0}
    command = host._run_command_definition

    def generate(*args, **kwargs):
        if not model["loaded"]:
            model["loaded"] = True
            model["loads"] += 1
        return command(*args, **kwargs)

    monkeypatch.setattr(host, "_run_command_definition", generate)
    try:
        assert run("first")["task"]["status"] == "completed"
        backend = host.managed_tool_session.current_adapter.backend
        pid = backend.child.pid
        binding = host.managed_tool_session.current_binding
        first_timer = timers.live()
        assert first_timer.seconds == 1800 and model["loads"] == 1
        assert host.warm_holds.status()[0]["expires_at"] == "1970-01-01T00:30:00+00:00"

        # Model-unload control is the fixture's stand-in for the native adapter.
        release = backend.release
        def unload(*, reason):
            model["loaded"] = False
            model["unloads"] += 1
            return release(reason=reason)
        backend.release = unload
        from astrid.core.generation import engine_runner
        def unexpected_setup(*args, **kwargs):
            pytest.fail("warm run attempted engine install, start, or health wait")
        monkeypatch.setattr(engine_runner, "install_engine", unexpected_setup)
        monkeypatch.setattr(engine_runner._Runner, "start", unexpected_setup)
        monkeypatch.setattr(engine_runner._Runner, "check_health", unexpected_setup)
        now[0] = 1799
        assert run("second")["task"]["status"] == "completed"
        assert host.managed_tool_session.current_adapter.backend is backend
        assert host.managed_tool_session.current_binding == binding
        assert backend.child.pid == pid and backend.child.poll() is None
        assert len(starts) == 1
        # The host constructs a candidate adapter but retains the original resident one.
        assert events.count("construct") == 2 and len(backends) == 2
        assert model == {"loaded": True, "loads": 1, "unloads": 0}
        assert first_timer.cancelled and timers.live() is not first_timer
        assert timers.live().seconds == 1800
        assert host.warm_holds.status()[0]["expires_at"] == "1970-01-01T00:59:59+00:00"
        first_timer.callback()
        assert model["unloads"] == 0

        now[0] = 3599
        timers.fire()
        assert events[-1] == "release:idle" and model["unloads"] == 1
        assert not model["loaded"] and not host.managed_tool_session.occupied
        assert backend.child.pid == pid and backend.child.poll() is None
        assert run("third")["task"]["status"] == "completed"
        assert model == {"loaded": True, "loads": 2, "unloads": 1}
        assert host.managed_tool_session.current_binding == binding
        assert len(backends) == 3 and backends[2].child is None and len(starts) == 1
        assert timers.live().seconds == 1800
        assert [row[0] for row in runtime.settlements] == ["first", "second", "third"]
    finally:
        host.warm_holds.close()
        host.managed_tool_session.close()
        for backend in backends:
            if backend.child is not None and backend.child.poll() is None:
                backend.child.terminate()
                backend.child.wait(timeout=5)
