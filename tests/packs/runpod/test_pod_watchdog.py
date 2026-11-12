"""Pod billing deadlines, with fake clocks, provider and transport only."""
from __future__ import annotations

import importlib
import io
import json
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from tests.packs.runpod.test_generation_preparation import retained as retained  # noqa: F401
from tests.packs.runpod.test_remote_placement import sources as sources  # noqa: F401
from tests.packs.runpod.test_warm_session import (  # noqa: F401
    lease_selection_case as lease_selection_case, small_lease as small_lease,
)


def watchdog_module():
    return importlib.import_module("astrid.packs.runpod.pod_watchdog")


def write(path: Path, deadline: float, generation: int):
    path.write_text(json.dumps({"deadline": deadline, "generation": generation}))


def test_no_renewal_terminates_directly_at_deadline(tmp_path):
    module = watchdog_module()
    calls = []
    watch = module.Watchdog(tmp_path / "deadline", 100, terminate=lambda: calls.append("terminate"))
    assert not watch.tick(99)
    assert calls == []
    assert watch.tick(100)
    assert calls == ["terminate"]


def test_renewal_moves_deadline_and_older_generation_cannot_move_it_back(tmp_path):
    module = watchdog_module()
    path = tmp_path / "deadline"
    calls = []
    watch = module.Watchdog(path, 100, terminate=lambda: calls.append("terminate"))
    write(path, 120, 2)
    assert not watch.tick(99)
    write(path, 80, 1)
    assert not watch.tick(100)
    write(path, 500, 2)
    assert watch.tick(120)
    assert calls == ["terminate"]


@pytest.mark.parametrize("contents", [None, "invalid", '{"deadline": true, "generation": 4}'])
def test_missing_or_unreadable_file_keeps_last_deadline(tmp_path, contents):
    module = watchdog_module()
    path = tmp_path / "deadline"
    watch = module.Watchdog(path, 100, terminate=lambda: None)
    write(path, 110, 1)
    assert not watch.tick(90)
    if contents is None:
        path.unlink()
    else:
        path.write_text(contents)
    assert watch.tick(110)


@pytest.mark.parametrize("missing", ["RUNPOD_POD_ID", "RUNPOD_API_KEY"])
def test_missing_pod_identity_refuses_without_starting_process(tmp_path, monkeypatch, missing):
    module = watchdog_module()
    monkeypatch.setenv("RUNPOD_POD_ID", "fake-pod")
    monkeypatch.setenv("RUNPOD_API_KEY", "fake-pod-key")
    monkeypatch.delenv(missing)
    with pytest.raises(module.PodWatchdogError, match=missing):
        module.ensure_running(tmp_path / "deadline", 100)


def test_termination_retries_with_bounded_backoff_until_accepted(tmp_path, monkeypatch):
    module = watchdog_module()
    monkeypatch.setenv("RUNPOD_POD_ID", "fake-pod")
    monkeypatch.setenv("RUNPOD_API_KEY", "fake-pod-key")
    monkeypatch.setattr(module, "IDENTITY_LOG_PATH", tmp_path / "engine.log")
    monkeypatch.setattr(module.time, "time", lambda: 100)
    sleeps, methods = [], []
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    def request(req, timeout):
        methods.append(req.get_method())
        assert req.full_url == "https://api.runpod.io/v2/pods/fake-pod"
        assert req.data is None
        if req.get_method() == "GET":
            return io.BytesIO(b'{"actions":["terminate"]}')
        assert timeout == 10
        if methods.count("DELETE") <= 8:
            raise URLError("unavailable")
        return io.BytesIO(b"")

    monkeypatch.setattr(module, "urlopen", request)
    module.run(tmp_path / "deadline", 100)
    assert methods == ["GET"] + ["DELETE"] * 9
    assert sleeps == [1, 2, 4, 8, 16, 30, 30, 30]
    assert len((tmp_path / "engine.log").read_text().splitlines()) == 1


@pytest.mark.parametrize(("result", "authorised"), [
    ({"actions": ["terminate"]}, "yes"),
    ({"actions": ["start"]}, "no"),
    ({}, "unknown"),
    (401, "no"),
    (403, "no"),
    (503, "unknown"),
])
def test_identity_report_contains_names_only_and_read_only_authorisation(
        tmp_path, monkeypatch, result, authorised):
    module = watchdog_module()
    monkeypatch.setenv("RUNPOD_POD_ID", "fake-pod")
    monkeypatch.setenv("RUNPOD_API_KEY", "fake-pod-key")
    log = tmp_path / "engine.log"
    monkeypatch.setattr(module, "IDENTITY_LOG_PATH", log)

    def request(req, timeout):
        assert req.get_method() == "GET"
        assert req.full_url == "https://api.runpod.io/v2/pods/fake-pod"
        assert timeout == 2
        if isinstance(result, int):
            raise HTTPError(req.full_url, result, "fake-pod-key", {}, None)
        return io.BytesIO(json.dumps({**result, "env": {"RUNPOD_API_KEY": "fake-pod-key"}}).encode())

    monkeypatch.setattr(module, "urlopen", request)
    module._report_identity()
    assert log.read_text() == (
        "pod_watchdog: identity=RUNPOD_POD_ID,RUNPOD_API_KEY "
        f"termination_authorised={authorised} probe=GET-own-pod-actions\n")
    assert "fake-pod" not in log.read_text()
    assert "fake-pod-key" not in log.read_text()


def test_first_start_identity_line_is_collected_by_host(retained, tmp_path, monkeypatch):
    from tests.packs.runpod.test_generation_preparation import _claim, _request_for_job

    module = watchdog_module()
    remote_log = module.IDENTITY_LOG_PATH
    log = tmp_path / "engine.log"
    monkeypatch.setenv("RUNPOD_POD_ID", "fake-pod")
    monkeypatch.setenv("RUNPOD_API_KEY", "fake-pod-key")
    monkeypatch.setattr(module, "IDENTITY_LOG_PATH", log)
    monkeypatch.setattr(module, "urlopen", lambda *a, **kw: io.BytesIO(b'{"actions":["terminate"]}'))
    # The real first-start path writes its line before acknowledging readiness.
    with monkeypatch.context() as clock:
        clock.setattr(module.time, "time", lambda: 100)
        module.run(tmp_path / "deadline", 100)
    line = log.read_text()
    monkeypatch.setattr(module, "IDENTITY_LOG_PATH", remote_log)
    c = retained
    original = c.pod.exec

    def execute(command, timeout=600):
        if command == f"grep '^{module.IDENTITY_MARK}' {remote_log} | tail -n 1":
            return 0, line, ""
        return original(command, timeout)

    monkeypatch.setattr(c.pod, "exec", execute)
    _claim(c)
    request, consent = _request_for_job(c)
    prepared = c.adapter.prepare(request, consent)
    collected = prepared.state.local_out / "logs" / "engine.log"
    c.pod.files.clear()  # The host copy survives loss of the pod.
    assert collected.read_text() == line
    assert "RUNPOD_POD_ID,RUNPOD_API_KEY" in collected.read_text()
    assert "fake-pod" not in collected.read_text()
    assert "fake-pod-key" not in collected.read_text()


@pytest.mark.parametrize("writer", ["claim", "keep", "settle"])
def test_host_writes_deadline_at_each_transition(retained, monkeypatch, writer):
    module = watchdog_module()
    c = retained
    writes = []
    original = module.write_deadline

    def capture(ops, deadline, generation):
        writes.append((deadline, generation))
        return original(ops, deadline, generation)

    monkeypatch.setattr(module, "write_deadline", capture)
    invocation = c.host.acquire(c.grant, requested_idle_seconds=10)
    if writer != "claim":
        writes.clear()
        if writer == "settle":
            c.host.settle(invocation)
        else:
            c.host.keep("pod-1", replace(c.grant, invocation_id="keep-watchdog"))
    saved = c.read()
    lease = saved["lease"]
    deadline = lease["idle_expires_at"] if lease["state"] == "idle" else lease["paid_expires_at"]
    assert len(writes) == 1
    assert writes[0][0] == deadline
    assert saved["self_stop"] == {"deadline": deadline, "generation": writes[0][1],
                                  "acknowledged": True}


def test_claim_releases_pod_when_watchdog_reports_missing_identity(retained, monkeypatch):
    module = watchdog_module()
    c = retained

    def refuse(ops, deadline, generation):
        raise module.PodWatchdogError("RunPod claim refused: pod watchdog missing RUNPOD_API_KEY; use a template with pod identity and claim again")

    monkeypatch.setattr(module, "write_deadline", refuse)
    with pytest.raises(module.PodWatchdogError, match="missing RUNPOD_API_KEY;.*claim again"):
        c.host.acquire(c.grant, requested_idle_seconds=10)
    assert c.deletes == ["pod-1"]
    assert c.read()["state"] == "cleanup_complete"
    assert c.creates == 1


@pytest.mark.parametrize("provider_state", ["gone", "stopped"])
def test_restart_after_self_stop_settles_released_without_replacement(retained, monkeypatch, provider_state):
    watchdog_module()
    from astrid.packs.runpod import worker_owner
    c = retained
    invocation = c.host.acquire(c.grant, requested_idle_seconds=10)
    c.host.settle(invocation)
    deadline = c.read()["self_stop"]["deadline"]
    c.host.close()
    c.clock.now = deadline + 1
    if provider_state == "gone":
        c.pods.clear()
    else:
        c.pods[0].update(desiredStatus="EXITED", actualStatus="EXITED")
    monkeypatch.setattr(worker_owner, "provider_pod_state", lambda *args: provider_state)
    owner = c.fresh()
    owner.recover()
    saved = c.read()
    assert saved["state"] == "cleanup_complete"
    assert saved["lease"]["ended"] == "self_stop"
    assert c.creates == 1
