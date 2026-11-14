"""Local managed VRAM warm-down with explicit timers and CPU-only fake engines."""
from __future__ import annotations

import json
import logging
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.execution.guards import read_evidence_status
from astrid.core.execution.warm_hold import DEFAULT_IDLE_SECONDS
from tests.core.execution.test_warm_hold import Timers
from tests.core.generation.test_engine_custody import (
    _call,
    _request,
)
from tests.core.generation.test_engine_custody import (
    native_scratch as native_scratch,
)
from tests.core.generation.test_engine_custody import (
    session as session,
)


class Supervisor:
    """Like engine_custody, serialize control operations under one work lock."""

    def __init__(self):
        self.work = threading.Lock()
        self.events = []
        self.error = None
        self.result = {"status": "completed", "models_unloaded": True}
        self.releasing = threading.Event()
        self.continue_release = threading.Event()
        self.continue_release.set()

    def release_owned(self, *, deadline):
        assert deadline > 0
        with self.work:
            self.events.append("release")
            self.releasing.set()
            assert self.continue_release.wait(5), "release fixture did not drain"
            if self.error:
                raise self.error
            return self.result

    def run(self, task_id):
        with self.work:
            self.events.append(task_id)
            return {"status": "succeeded", "task_id": task_id}


def task(task_id="job", *, kind="local", idle=12, wrapped=False):
    inputs = {}
    if kind == "attached":
        inputs["comfy_url"] = "http://user-engine.invalid:8188"
    elif kind is not None:
        policy = {"kind": kind, "device": None} if kind == "local" else {"kind": kind}
        if idle is not None:
            policy["idle_timeout_s"] = idle
        inputs["placement_policy"] = json.dumps(policy)
    spec = {"inputs": inputs}
    return {"task": {"id": task_id, "capability": "vibecomfy.run",
                     "spec": {"spec": spec} if wrapped else spec}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    host = GenericPackHost(pack_roots=[tmp_path], attempt_root=tmp_path / "attempt")
    timers = Timers()
    idle = host._local_engine_idle = type(host._local_engine_idle)(
        timer=timers, on_change=host._publish_evidence_status)
    host.evidence_status_path = tmp_path / "evidence.json"
    supervisor = Supervisor()

    def execute(value, *, _terminal, **kwargs):
        # Preparation supplies the supervisor already bound to this installation.
        # The real run_task boundary owns admission and completion around this seam.
        if host._local_engine_idle.status()["active_jobs"]:
            host._local_engine_idle.retain(supervisor)
        result = supervisor.run(value["task"]["id"])
        _terminal["recorded"] = True
        return result

    monkeypatch.setattr(host, "_run_task_impl", execute)
    monkeypatch.setattr(host, "_write_task_result", lambda terminal: None)
    host.client = SimpleNamespace(fail=Mock(), settle=Mock())
    yield host, idle, timers, supervisor
    host.shutdown()


def run(host, value=None):
    return host.run_task(value or task(), lease_token="lease")


def test_idle_releases_once_and_uses_the_placement_timeout(world):
    host, idle, timers, supervisor = world
    assert run(host)["status"] == "succeeded"
    assert timers.live().seconds == 12
    callback = timers.live().callback
    timers.fire()
    callback()  # A spent deadline cannot release twice.
    assert supervisor.events == ["job", "release"]
    assert timers.live() is None
    assert read_evidence_status(host.evidence_status_path)["local_engine_idle"]["last_release"] == "completed"


def test_new_job_cancels_expiry_and_starts_a_fresh_idle_period(world):
    host, idle, timers, supervisor = world
    run(host)
    stale = timers.live()
    assert run(host, task("next", idle=7))["status"] == "succeeded"
    assert stale.cancelled
    stale.callback()
    assert supervisor.events == ["job", "next"]
    assert timers.live().seconds == 7
    timers.fire()
    assert supervisor.events == ["job", "next", "release"]


@pytest.mark.parametrize("queued", [False, True], ids=["running", "queued-for-host-slot"])
def test_expiry_checks_busy_immediately_before_release(world, monkeypatch, queued):
    host, idle, timers, supervisor = world
    run(host)
    expiry_ready, inspect_busy, executing, finish_job = (threading.Event() for _ in range(4))
    original = idle._release_if_idle

    def pause_before_busy_check(generation):
        expiry_ready.set()
        assert inspect_busy.wait(5)
        original(generation)

    monkeypatch.setattr(idle, "_release_if_idle", pause_before_busy_check)
    expiry = threading.Thread(target=timers.fire)
    expiry.start()
    assert expiry_ready.wait(5)
    results = []

    def execute(value, *, _terminal, **kwargs):
        executing.set()
        assert finish_job.wait(5)
        _terminal["recorded"] = True
        return supervisor.run("next")

    monkeypatch.setattr(host, "_run_task_impl", execute)
    if queued:
        host._session_task_lock.acquire()
    job = threading.Thread(target=lambda: results.append(run(host, task("next"))))
    job.start()
    try:
        # begin() publishes evidence before a queued job can acquire the slot.
        with idle._condition:
            assert idle._condition.wait_for(lambda: idle.status()["active_jobs"] == 1, timeout=5)
        if not queued:
            assert executing.wait(5)
        inspect_busy.set()
        expiry.join(5)
        assert not expiry.is_alive()
        assert supervisor.events == ["job"], "busy engine must not receive release_owned"
    finally:
        inspect_busy.set()
        if queued:
            host._session_task_lock.release()
        finish_job.set()
        job.join(5)
        expiry.join(5)
    assert not job.is_alive() and results[0]["status"] == "succeeded"
    assert timers.live() is not None


@pytest.mark.parametrize("kind", ["attached", "runpod"])
def test_other_placements_do_not_arm_a_local_timer(world, kind):
    host, idle, timers, supervisor = world
    run(host, task(kind=kind))
    assert idle.status()["active_jobs"] == 0
    assert timers.armed == []
    assert supervisor.events == ["job"]


@pytest.mark.parametrize("failure", ["exception", "failed-result"])
def test_release_failure_is_warning_evidence_and_next_job_succeeds(world, caplog, failure):
    host, idle, timers, supervisor = world
    run(host)
    if failure == "exception":
        supervisor.error = OSError("fixture offline")
    else:
        supervisor.result = {"status": "failed", "models_unloaded": False}
    with caplog.at_level(logging.WARNING):
        timers.fire()
    assert any(record.levelno == logging.WARNING and "local managed engine idle release" in record.message
               for record in caplog.records)
    evidence = read_evidence_status(host.evidence_status_path)["local_engine_idle"]
    assert evidence["last_release"] == "failed" and evidence["warning"]
    supervisor.error = None
    assert run(host, task("after-failure"))["status"] == "succeeded"
    assert supervisor.events == ["job", "release", "after-failure"]


def test_host_shutdown_cancels_pending_timer_without_releasing(world):
    host, idle, timers, supervisor = world
    run(host)
    pending = timers.live()
    host.shutdown()
    assert pending.cancelled
    pending.callback()
    assert supervisor.events == ["job"]
    assert idle.status()["closed"] is True


def test_job_admitted_during_release_still_runs_with_supervisor_serialization(world):
    host, idle, timers, supervisor = world
    run(host)
    supervisor.continue_release.clear()
    expiry = threading.Thread(target=timers.fire)
    expiry.start()
    assert supervisor.releasing.wait(5)
    results = []
    job = threading.Thread(target=lambda: results.append(run(host, task("during-release"))))
    job.start()
    try:
        with idle._condition:
            assert idle._condition.wait_for(lambda: idle.status()["active_jobs"] == 1, timeout=5)
        assert job.is_alive(), "admission must progress while release_owned holds the supervisor lock"
    finally:
        supervisor.continue_release.set()
        expiry.join(5)
        job.join(5)
    assert not expiry.is_alive() and not job.is_alive()
    assert results == [{"status": "succeeded", "task_id": "during-release"}]
    assert supervisor.events == ["job", "release", "during-release"]
    assert timers.live().seconds == 12


@pytest.mark.parametrize("kind", [None, "local"])
def test_omitted_idle_uses_the_existing_default(world, monkeypatch, kind):
    monkeypatch.delenv("ASTRID_WARM_IDLE_SECONDS", raising=False)
    host, idle, timers, supervisor = world
    run(host, task(kind=kind, idle=None, wrapped=True))
    assert timers.live().seconds == DEFAULT_IDLE_SECONDS


def test_parallel_host_slots_share_one_engine_idle_controller(world):
    host, idle, timers, supervisor = world
    host.client.fork_for_host_slot = lambda: SimpleNamespace()
    slot = host._new_claim_slot()
    assert slot._local_engine_idle is idle
    run(host)
    assert slot._admit_local_engine(task("queued")["task"])
    assert timers.live() is None
    slot.shutdown()
    assert idle.status()["closed"] is False, "a child slot cannot close the shared engine timer"
    idle.finish()
    timers.fire()
    assert supervisor.events == ["job", "release"]


def test_sdk_preserves_local_idle_timeout_at_workflow_admission(tmp_path, monkeypatch):
    from astrid.core.generation import preflight
    from astrid.core.generation.backends.comfy_target import ComfyTargetClient
    from astrid.sdk.generation import GenerationFacade
    from tests.sdk.test_generation_target import storage_client, terminal_result

    monkeypatch.setattr(preflight, "_registered_managed_engine", lambda: {
        "owner": "astrid", "endpoint": "http://127.0.0.1:8188", "root": str(tmp_path / "engine")})
    monkeypatch.setattr(ComfyTargetClient, "request_json", Mock(side_effect=AssertionError("network used")))
    admitted = Mock(return_value=terminal_result(tmp_path))
    monkeypatch.setattr(GenerationFacade, "workflow", admitted)
    client, _ = storage_client()
    result = GenerationFacade().image(placement={"kind": "local", "policy": {"idle_timeout_s": 23}},
                                      client=client, project="demo", prompt="one boat")
    assert result.ok
    assert admitted.call_args.kwargs["placement_policy"] == {
        "kind": "local", "device": None, "idle_timeout_s": 23}


@pytest.mark.parametrize("queue", ["busy", "pending"])
def test_engine_queue_busy_consumes_deadline_until_next_task_end(world, session, queue):  # noqa: F811
    host, idle, timers, _ = world
    client, payload, recipe, root, mode, started = session
    _call(client, payload)
    run(host)
    idle.retain(client)
    mode.write_text(queue)
    timers.fire()
    assert idle.status()["last_release"] == "busy"
    assert idle.status()["release_record"] == {"status": "busy"}
    assert timers.live() is None
    assert not (root / "release-posts").exists()
    mode.write_text("healthy")
    run(host, task("next"))
    idle.retain(client)
    timers.fire()
    assert (root / "release-posts").read_text().splitlines() == ["/api/free"]


def test_owned_engine_runs_after_idle_and_records_vram_in_declared_log(world, session, tmp_path, monkeypatch):  # noqa: F811
    from astrid.core._shared import run_logs
    from astrid.core.generation.engine_custody import declare_engine_log

    host, idle, timers, _ = world
    client, payload, recipe, root, mode, started = session
    _call(client, payload)
    engine_pid = started.read_text()
    logs_root = tmp_path / "declared-run"
    declare_engine_log(logs_root, str(root), recipe.engine)

    def execute(value, *, _terminal, **kwargs):
        idle.retain(client)
        prompt_id = value["task"]["id"]
        code = f'''
from astrid.core.generation.backends.comfy_target import ComfyTargetClient, ComfyTarget
engine = ComfyTargetClient(ComfyTarget("http://127.0.0.1:{recipe.start.port}", "managed"))
response = engine.submit_prompt({{"client_id": {prompt_id!r}, "prompt": {{}}}})
assert response["prompt_id"] == {prompt_id!r}
history = engine.request_json("/history/" + response["prompt_id"])
assert history[response["prompt_id"]]["status"]["completed"] is True
'''
        result = client.run(_request(root, code))
        assert result.exit_code == 0, result.stderr
        _terminal["recorded"] = True
        return {"status": "succeeded"}

    monkeypatch.setattr(host, "_run_task_impl", execute)
    assert run(host)["status"] == "succeeded"
    timers.fire()
    record = idle.status()["release_record"]
    assert record == {"status": "completed", "models_unloaded": True,
                      "vram_total_bytes": 24000, "vram_free_before_bytes": 20000,
                      "vram_free_after_bytes": 20000}
    engine_decl, engine_log = next((decl, path) for decl, path in run_logs.read_declared(logs_root)
                                   if decl.name == "engine")
    lines = engine_log.read_bytes()[engine_decl.start:].decode().splitlines()
    assert record in [json.loads(line) for line in lines if line.startswith("{")]
    assert run(host, task("after-release"))["status"] == "succeeded"
    assert (root / "prompts").read_text().splitlines() == ["job", "after-release"]
    assert started.read_text() == engine_pid, "idle release must retain the engine process"
    assert (root / "release-posts").read_text().splitlines() == ["/api/free"]


def test_shutdown_waits_for_an_already_running_release(world):
    host, idle, timers, supervisor = world
    run(host)
    supervisor.continue_release.clear()
    expiry = threading.Thread(target=timers.fire)
    expiry.start()
    assert supervisor.releasing.wait(5)
    closed = threading.Event()
    shutdown = threading.Thread(target=lambda: (host.shutdown(), closed.set()))
    shutdown.start()
    try:
        with idle._condition:
            assert idle._condition.wait_for(lambda: idle.status()["closed"], timeout=5)
        assert not closed.is_set()
    finally:
        supervisor.continue_release.set()
        expiry.join(5)
        shutdown.join(5)
    assert closed.is_set() and not expiry.is_alive() and not shutdown.is_alive()
    assert timers.live() is None
    assert idle.status()["last_release"] == "completed"


def test_environment_idle_timeout_is_used_when_placement_omits_it(world, monkeypatch):
    host, idle, timers, supervisor = world
    monkeypatch.setenv("ASTRID_WARM_IDLE_SECONDS", "19")
    run(host, task(idle=None))
    assert timers.live().seconds == 19


def test_lifecycle_idle_timeout_wins_over_placement_and_environment(world, monkeypatch):
    from astrid.core.execution import generic_host

    host, idle, timers, supervisor = world
    monkeypatch.setenv("ASTRID_WARM_IDLE_SECONDS", "19")
    # The canonical contract validator has its own tests; this boundary only
    # checks which already validated idle policy the controller consumes.
    monkeypatch.setattr(generic_host, "_execution_contract",
                        lambda *args, **kwargs: {"lifecycle": {"idle_timeout_seconds": 3}})
    run(host)
    assert timers.live().seconds == 3
