"""Pod billing deadlines, with fake clocks, provider and transport only."""
from __future__ import annotations

import ast
import base64
import importlib
import io
import json
import shlex
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from tests.packs.runpod.test_generation_preparation import retained as retained  # noqa: F401
from tests.packs.runpod.test_remote_placement import sources as sources  # noqa: F401
from tests.packs.runpod.test_warm_session import (  # noqa: F401
    lease_selection_case as lease_selection_case, small_lease as small_lease,
)


class ProviderResponse(io.BytesIO):
    status: int


def provider_response(body: bytes, status: int = 200):
    response = ProviderResponse(body)
    response.status = status
    return response


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
            return provider_response(b'{"actions":["terminate"]}')
        assert timeout == 10
        if methods.count("DELETE") <= 8:
            raise URLError("unavailable")
        return provider_response(b"")

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
        return provider_response(json.dumps({**result, "env": {"RUNPOD_API_KEY": "fake-pod-key"}}).encode())

    monkeypatch.setattr(module, "urlopen", request)
    module._report_identity()
    assert log.read_text() == (
        "pod_watchdog: identity=RUNPOD_POD_ID,RUNPOD_API_KEY "
        f"termination_authorised={authorised} probe=GET-own-pod-actions "
        f"probe_status={result if isinstance(result, int) else 200}"
        + (" watchdog=unavailable custody=host-only" if authorised == "unknown" else "") + "\n")
    assert "fake-pod" not in log.read_text()
    assert "fake-pod-key" not in log.read_text()



@pytest.mark.parametrize(("failure", "status"), [
    ("malformed-json", 200), ("transport", 0), ("missing-identity", 0),
])
def test_identity_probe_records_observed_status_when_unavailable(tmp_path, monkeypatch, failure, status):
    module = watchdog_module()
    monkeypatch.setenv("RUNPOD_POD_ID", "fake-pod")
    monkeypatch.setenv("RUNPOD_API_KEY", "fake-pod-key")
    log = tmp_path / "engine.log"
    monkeypatch.setattr(module, "IDENTITY_LOG_PATH", log)

    def request(*args, **kwargs):
        if failure == "transport":
            raise URLError("private transport detail")
        assert failure == "malformed-json"
        return provider_response(b"private malformed JSON", status=200)

    if failure == "missing-identity":
        monkeypatch.delenv("RUNPOD_API_KEY")
    monkeypatch.setattr(module, "urlopen", request)
    expected = "no" if failure == "missing-identity" else "unknown"
    assert module._report_identity() == expected
    line = log.read_text()
    assert f"probe_status={status}" in line
    assert "private" not in line
    if expected == "unknown":
        assert "watchdog=unavailable custody=host-only" in line


def test_identity_probe_does_not_infer_status_from_mocked_body(tmp_path, monkeypatch):
    module = watchdog_module()
    monkeypatch.setenv("RUNPOD_POD_ID", "fake-pod")
    monkeypatch.setenv("RUNPOD_API_KEY", "fake-pod-key")
    log = tmp_path / "engine.log"
    monkeypatch.setattr(module, "IDENTITY_LOG_PATH", log)
    monkeypatch.setattr(module, "_request", lambda *args, **kwargs: {"actions": ["terminate"]})
    assert module._report_identity() == "yes"
    assert "probe_status=0" in log.read_text()


def test_first_start_identity_line_is_collected_by_host(retained, tmp_path, monkeypatch):
    from tests.packs.runpod.test_generation_preparation import _claim, _request_for_job

    module = watchdog_module()
    remote_log = module.IDENTITY_LOG_PATH
    log = tmp_path / "engine.log"
    monkeypatch.setenv("RUNPOD_POD_ID", "fake-pod")
    monkeypatch.setenv("RUNPOD_API_KEY", "fake-pod-key")
    monkeypatch.setattr(module, "IDENTITY_LOG_PATH", log)
    monkeypatch.setattr(module, "urlopen", lambda *a, **kw: provider_response(b'{"actions":["terminate"]}'))
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


def test_transported_terminate_template_equals_exact_release_template(monkeypatch):
    from astrid.packs.runpod import h3_release

    module = watchdog_module()
    requests, release_calls = [], []
    provider = h3_release.RunPodProvider.__new__(h3_release.RunPodProvider)
    monkeypatch.setattr(provider, "request", lambda *a: release_calls.append(a))

    def execute(command, timeout):
        loader = ast.parse(shlex.split(command)[2])
        encoded = next(node.args[0].value for node in ast.walk(loader)
                       if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                       and node.func.attr == "b64decode")
        scope = {"__name__": "__pod_watchdog__"}
        exec(compile(base64.b64decode(encoded), "pod-watchdog.py", "exec"), scope)
        assert scope["POD_URL_TEMPLATE"] == h3_release.POD_URL_TEMPLATE
        assert scope["POD_TERMINATE_METHOD"] == h3_release.POD_TERMINATE_METHOD
        scope["urlopen"] = lambda req, **kw: requests.append(req) or io.BytesIO(b"")
        scope["_request"]("own/id", "fake-only")
        return 0, "", ""

    module.write_deadline(SimpleNamespace(exec=execute), 100, 1)
    provider.delete("own/id")
    assert release_calls == [("DELETE", "https://api.runpod.io/v2/pods/own%2Fid")]
    assert (requests[0].get_method(), requests[0].full_url) == release_calls[0]


def test_publication_writes_valid_new_bound_before_replacement_starts(tmp_path, monkeypatch):
    module = watchdog_module()
    path = tmp_path / "deadline"
    write(path, 100, 1)
    starts, deletes = [], []

    def start(path, deadline):
        starts.append(deadline)
        assert module._deadline(json.loads(path.read_text())) == (200, 2)
        replacement = module.Watchdog(path, deadline, terminate=lambda: deletes.append(True))
        assert not replacement.tick(100)

    monkeypatch.setattr(module, "ensure_running", start)
    module.publish(path, 200, 2)
    assert starts == [200] and deletes == []


def test_renewal_wins_between_initial_read_and_locked_expiry_decision(tmp_path, monkeypatch):
    module = watchdog_module()
    path = tmp_path / "deadline"
    write(path, 100, 1)
    deletes = []
    watch = module.Watchdog(path, 100, terminate=lambda: deletes.append(True))
    monkeypatch.setattr(module, "ensure_running", lambda *args: None)
    refresh = watch._refresh
    reads = []

    def interleave():
        refresh()
        reads.append(watch.generation)
        if len(reads) == 1:
            module.publish(path, 200, 2)

    monkeypatch.setattr(watch, "_refresh", interleave)
    assert not watch.tick(100)
    assert reads == [1, 2]
    assert not deletes and not path.with_suffix(".terminating").exists()


def test_termination_commitment_wins_and_refuses_later_publication(tmp_path, monkeypatch):
    import threading

    module = watchdog_module()
    path = tmp_path / "deadline"
    write(path, 100, 1)
    attempted, finished = threading.Event(), threading.Event()
    causes, starts = [], []
    monkeypatch.setattr(module, "ensure_running", lambda *a: starts.append(True))
    flock = module.fcntl.flock

    def lock(fd, operation):
        if threading.current_thread() is publisher:
            attempted.set()
        flock(fd, operation)

    def renew():
        try:
            module.publish(path, 200, 2)
        except module.PodWatchdogError as exc:
            causes.append(str(exc))
        finally:
            finished.set()

    publisher = threading.Thread(target=renew)
    monkeypatch.setattr(module.fcntl, "flock", lock)

    def delete():
        assert path.with_suffix(".terminating").exists()
        publisher.start()
        assert attempted.wait(2)
        assert not finished.is_set()  # DELETE still owns the publication lock.

    watch = module.Watchdog(path, 100, terminate=delete)
    assert watch.tick(100)
    publisher.join(timeout=2)
    assert not publisher.is_alive()
    assert causes == ["termination_committed"] and not starts
    assert json.loads(path.read_text()) == {"deadline": 100, "generation": 1}


@pytest.mark.parametrize("probe", [401, 403, {"actions": ["start"]}, 503, {}])
def test_startup_authorisation_controls_claim_and_exact_release(
        request, tmp_path, monkeypatch, capsys, probe):
    module = watchdog_module()
    c = request.getfixturevalue("retained")
    path = tmp_path / "deadline"
    log = tmp_path / "engine.log"
    monkeypatch.setattr(module, "DEADLINE_PATH", path)
    monkeypatch.setattr(module, "IDENTITY_LOG_PATH", log)
    monkeypatch.setenv("RUNPOD_POD_ID", "fake-pod")
    monkeypatch.setenv("RUNPOD_API_KEY", "fake-pod-key")
    denied = probe in (401, 403) or isinstance(probe, dict) and "actions" in probe

    def probe_request(req, **kwargs):
        assert req.get_method() == "GET"
        assert req.full_url == "https://api.runpod.io/v2/pods/fake-pod"
        if isinstance(probe, int):
            raise HTTPError(req.full_url, probe, "fake-pod-key", {}, None)
        return provider_response(json.dumps(probe).encode())

    def spawn(*args, **kwargs):
        try:
            module.run(path, c.read()["self_stop"]["deadline"])
        except module.PodWatchdogError:
            pass  # Simulate the child exiting after writing its fixed cause.
        return SimpleNamespace(poll=lambda: 1, wait=lambda **kw: 1, kill=lambda: None)

    original = c.pod.exec

    def execute(command, timeout=600):
        if command.startswith("python3 -c "):
            saved = c.read()["self_stop"]
            output = io.StringIO()
            with redirect_stdout(output):
                try:
                    module.main(["publish", saved["deadline"], saved["generation"]])
                except SystemExit as exc:
                    return exc.code, output.getvalue(), ""
            return 0, output.getvalue(), ""
        return original(command, timeout)

    monkeypatch.setattr(module, "urlopen", probe_request)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(c.pod, "exec", execute)
    if denied:
        with pytest.raises(module.PodWatchdogError, match="termination_denied") as error:
            c.host.acquire(c.grant, requested_idle_seconds=10)
        assert len(str(error.value).splitlines()) == 1
        assert "fake-pod" not in str(error.value)
        assert c.deletes == ["pod-1"]
        assert c.read()["state"] == "cleanup_complete"
    else:
        c.host.acquire(c.grant, requested_idle_seconds=10)
        assert c.read()["self_stop"]["acknowledged"] is False
        assert c.read()["state"] == "claimed" and not c.deletes
        assert c.host.deadline_timer is not None
        assert "host custody only" in capsys.readouterr().err
        assert "watchdog=unavailable custody=host-only" in log.read_text()
    assert c.creates == 1 and not c.pod.job_log
    assert "fake-pod" not in log.read_text()
    assert "fake-pod-key" not in log.read_text()


def test_initial_watchdog_precedes_readiness_and_provider_verification(request, monkeypatch):
    from astrid.packs.runpod import worker_preparation

    module = watchdog_module()
    c = request.getfixturevalue("retained")
    events = []
    publish, ready, verify = module.write_deadline, worker_preparation._wait_provider_ready, worker_preparation.observe_and_confirm_acquired_target

    def write_initial(ops, deadline, generation):
        assert c.read()["state"] == "allocated_unverified"
        assert c.read().get("target_identity_verified") is not True
        assert deadline == c.read()["self_stop"]["deadline"]
        events.append("watchdog")
        return publish(ops, deadline, generation)

    async def wait(*args):
        assert c.read()["self_stop"]["acknowledged"] is True
        events.append("readiness")
        return await ready(*args)

    async def confirm(*args, **kwargs):
        assert events == ["watchdog", "readiness"]
        events.append("verification")
        return await verify(*args, **kwargs)

    monkeypatch.setattr(module, "write_deadline", write_initial)
    monkeypatch.setattr(worker_preparation, "_wait_provider_ready", wait)
    monkeypatch.setattr(worker_preparation, "observe_and_confirm_acquired_target", confirm)
    c.host.acquire(c.grant, requested_idle_seconds=10)
    assert events == ["watchdog", "readiness", "verification"]


def test_host_releases_when_renewal_finds_termination_committed(request, monkeypatch):
    module = watchdog_module()
    c = request.getfixturevalue("retained")
    c.host.acquire(c.grant, requested_idle_seconds=10)
    monkeypatch.setattr(c.pod, "exec", lambda *a: (1, "pod_watchdog:termination_committed", ""))
    with pytest.raises(module.PodWatchdogError, match="termination_committed"):
        c.host.keep("pod-1", replace(c.grant, invocation_id="late-keep"))
    assert c.deletes == ["pod-1"]
    assert c.read()["lease"]["state"] == "releasing"
    assert c.read()["state"] == "cleanup_complete"


@pytest.mark.parametrize("acknowledged", [False, True])
def test_active_restart_retires_jobs_on_exact_absence_even_with_lost_ack(
        request, monkeypatch, acknowledged):
    from astrid.packs.runpod import worker_preparation
    from tests.packs.runpod.test_generation_preparation import _claim, _request_for_job

    c = request.getfixturevalue("retained")
    _claim(c)
    request, consent = _request_for_job(c)
    c.adapter.prepare(request, consent)
    record = c.read()
    assert record["lease"]["state"] == "active"
    record["lease"]["job"] = {"id": "a" * 32, "state": "running"}
    record["lease"]["preparation"].update(state="unresolved", job={"id": "b" * 32, "state": "running"})
    record["self_stop"]["acknowledged"] = acknowledged
    worker_preparation.replace_target_record(c.owner.path, record, operation_id=record["operation_id"])
    c.host.close()
    c.clock.now = record["self_stop"]["deadline"] + 1
    c.pods.clear()

    def dead_transport(*args, **kwargs):
        pytest.fail("dead pod must not require transport or job_cancel")

    monkeypatch.setattr(c.pod, "job_cancel", dead_transport)
    monkeypatch.setattr("astrid.packs.runpod.remote_placement.connect_claimed_for_preparation", dead_transport)
    owner = c.fresh()
    owner.recover()
    saved = c.read()
    assert saved["state"] == "cleanup_complete"
    assert saved["lease"]["job"]["state"] == "cancelled"
    assert saved["lease"]["preparation"]["job"]["state"] == "cancelled"
    assert saved["lease"]["ended"] == ("self_stop" if acknowledged else "provider_terminated")
    assert c.creates == 1 and not c.deletes
