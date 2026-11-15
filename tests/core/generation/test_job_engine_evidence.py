"""Job-scoped engine evidence over the local managed execution path."""
from __future__ import annotations

import importlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from astrid.core._shared import run_logs
from astrid.core.generation import engine_custody, managed_preparation
from astrid.core.generation.job_evidence import JobEvidence
from astrid.packs.vibecomfy import local_job, production_engine
from tests.sdk.test_release_generation_client import _FakeComfy
from tests.test_generic_host_result_json import host_case  # noqa: F401


@pytest.fixture
def job_harness(tmp_path, monkeypatch):
    workflow = tmp_path / 'workflow.py'
    workflow.write_text('pass')
    workflow.with_suffix('.vibe.json').write_text('{}')
    workflow.with_name('source.json').write_text('{}')
    root = tmp_path / 'engine'
    log = Path(engine_custody.engine_log_path(str(root), 'comfyui'))
    log.parent.mkdir(parents=True)
    log.write_text('old output\n')
    state = {'endpoint': '', 'fail': False, 'line': '', 'engine_started': False}
    real_preparation = managed_preparation.ManagedEnginePreparation

    class Preparation:
        recipe = SimpleNamespace(engine='comfyui', start=SimpleNamespace(port=1))
        verification_seconds = 1.25

        def __init__(self, **kwargs):
            self.root = str(root)
            self.recipe = SimpleNamespace(engine='comfyui', start=SimpleNamespace(port=urlsplit(state['endpoint']).port))

        def prepare(self, *args, **kwargs):
            waiting = getattr(self, "evidence_waiting", None)
            if waiting is not None:
                waiting()
            if state.get('waiting_line'):
                with log.open('a') as stream:
                    stream.write(state['waiting_line'] + '\n')
            callback = getattr(self, "evidence_start", None)
            if callback is not None:
                callback()
            if state.get("prepare_fail"):
                raise ValueError("preparation failed")
            if hasattr(real_preparation, "_start_engine"):
                from astrid.core.generation.engine_runner import _Runner

                def service(operation, **kwargs):
                    if operation == "start":
                        state["engine_started"] = True
                    return {"state": "owned" if state["engine_started"] else "absent"}

                runner = SimpleNamespace(launch_spec=lambda: {"fake": True}, service=service,
                                         service_owner="fake-owner", visibility=None)
                runner.start = lambda **kwargs: _Runner.start(runner, **kwargs)
                real_preparation._start_engine(self, runner)
            return {'endpoint': state['endpoint']}

        def _time_engine(self, operation):
            return real_preparation._time_engine(self, operation)

        def close(self, **kwargs):
            pass

    monkeypatch.setattr(managed_preparation, 'ManagedEnginePreparation', Preparation)
    monkeypatch.setattr(managed_preparation, 'preparation_scope', lambda report: __import__('contextlib').nullcontext())
    monkeypatch.setattr(production_engine, 'load_workflow_path', lambda *a: SimpleNamespace(resolved=object()))
    monkeypatch.setattr(production_engine, '_canonical_bundle_value', lambda value: SimpleNamespace(workflow=object()))
    monkeypatch.setattr(production_engine, 'loaded_workflow_execution_identity', lambda value: 'identity')
    action = importlib.import_module('astrid.packs.vibecomfy.actions.run.run')

    def execute(*args, **kwargs):
        with log.open('a') as stream:
            stream.write(state['line'] + '\n')
        if state['fail']:
            raise ValueError('job failed')
        return {'ok': True}

    monkeypatch.setattr(action, '_run_and_settle', execute)

    def run(out):
        return local_job.run_owned_action(workflow, out, task_identity='task', attempt_identity='attempt', align='check')

    def evidence(out):
        rows = {row['name']: row for row in run_logs.collect(out)}
        return ((out / 'logs' / rows['engine']['file']).read_text(),
                json.loads((out / 'logs' / rows['run']['file']).read_text()))

    return state, run, evidence, log


def test_consecutive_jobs_have_bounded_slices_and_run_record(tmp_path, job_harness):
    state, run, evidence, log = job_harness
    with _FakeComfy() as fake:
        state.update(endpoint=fake.url, line='first job')
        assert run(tmp_path / 'one') == {'ok': True}
        fake.vram_free = 19_000_000_000
        state['line'] = 'second job'
        assert run(tmp_path / 'two') == {'ok': True}
        with log.open('a') as stream:
            stream.write('after both jobs\n')
        first, record = evidence(tmp_path / 'one')
        second, second_record = evidence(tmp_path / 'two')
    assert second == 'second job\n'
    assert first == 'first job\n'
    assert record['vram_free_before_bytes'] == 20_000_000_000
    assert record['vram_free_after_bytes'] == 20_000_000_000
    assert second_record['vram_free_before_bytes'] == 19_000_000_000
    assert record['model_verification_seconds'] == 1.25


def test_unavailable_stats_does_not_fail_job(tmp_path, job_harness):
    state, run, evidence, _ = job_harness
    with _FakeComfy() as fake:
        fake.vram_free = None
        state.update(endpoint=fake.url, line='success')
        assert run(tmp_path / 'out') == {'ok': True}
        _, record = evidence(tmp_path / 'out')
    assert record['vram_free_before_bytes'] is None
    assert record['vram_free_after_bytes'] is None
    assert record['notes']


def test_failed_job_keeps_slice_and_record(tmp_path, job_harness):
    state, run, evidence, _ = job_harness
    with _FakeComfy() as fake:
        state.update(endpoint=fake.url, line='failed job', fail=True)
        with pytest.raises(ValueError, match='job failed'):
            run(tmp_path / 'out')
        log, record = evidence(tmp_path / 'out')
    assert log == 'failed job\n'
    assert record['vram_free_after_bytes'] == fake.vram_free
    assert record['model_verification_seconds'] == 1.25


def test_model_verification_timing_accumulates_on_failure(monkeypatch):
    prep = object.__new__(managed_preparation.ManagedEnginePreparation)
    prep.mode = 'check'
    prep.verification_seconds = 0.0
    prep.check = lambda: None
    prep._guard_runtime = lambda *args, **kwargs: None
    runner = SimpleNamespace(plan=SimpleNamespace(steps=[]), locks={}, commits={})
    receipt = SimpleNamespace(requirements_locks={}, source_commits={})
    ticks = iter([10.0, 10.5, 11.0, 12.5])
    monkeypatch.setattr(managed_preparation.time, 'monotonic', lambda: next(ticks))
    prep._verify_models = lambda manifest: None
    prep._verify_alignment(None, runner, {}, {}, receipt)

    def fail(manifest):
        raise ValueError('verification failed')

    prep._verify_models = fail
    with pytest.raises(ValueError, match='verification failed'):
        prep._verify_alignment(None, runner, {}, {}, receipt)
    assert prep.verification_seconds == 2.0


def test_waiting_for_engine_custody_excludes_previous_job(tmp_path, job_harness):
    state, run, evidence, _ = job_harness
    with _FakeComfy() as fake:
        state.update(endpoint=fake.url, waiting_line='previous job still running', line='this job')
        run(tmp_path / 'out')
        log, _ = evidence(tmp_path / 'out')
    assert log == 'this job\n'


@pytest.mark.parametrize("restart", ["truncate", "replace"])
def test_engine_restart_keeps_the_whole_new_log(tmp_path, monkeypatch, restart):
    preparation = SimpleNamespace(root=str(tmp_path / "engine"), recipe=SimpleNamespace(
        engine="comfyui", start=SimpleNamespace(port=1)), verification_seconds=0.0)
    log = Path(engine_custody.engine_log_path(preparation.root, "comfyui"))
    log.parent.mkdir(parents=True)
    log.write_text("previous job output\n" * 10)
    monkeypatch.setattr(JobEvidence, "_vram", lambda *args: None)
    out = tmp_path / "out"
    evidence = JobEvidence(out, preparation)
    payload = "new engine log\n" if restart == "truncate" else "replacement engine log\n" * 20
    if restart == "truncate":
        log.write_text(payload)
    else:
        replacement = log.with_suffix(".replacement")
        replacement.write_text(payload)
        replacement.replace(log)
    evidence.finish()
    rows = {row["name"]: row for row in run_logs.collect(out)}
    assert (out / "logs" / rows["engine"]["file"]).read_text() == payload
    record = json.loads((out / "logs" / rows["run"]["file"]).read_text())
    assert "engine log restarted during job" in record["notes"]


def test_local_job_retains_engine_log_when_evidence_construction_fails(tmp_path, monkeypatch, job_harness):
    state, run, _, log = job_harness

    def unavailable(*args, **kwargs):
        raise ValueError("evidence construction failed")

    monkeypatch.setattr("astrid.core.generation.job_evidence.JobEvidence", unavailable)
    with _FakeComfy() as fake:
        state.update(endpoint=fake.url, line="this job")
        assert run(tmp_path / "out") == {"ok": True}
    declarations = {decl.name: (decl, source) for decl, source in run_logs.read_declared(tmp_path / "out")}
    assert declarations["engine"][1] == log
    assert declarations["engine"][0].role == "engine"
    rows = {row["name"]: row for row in run_logs.collect(tmp_path / "out")}
    assert (tmp_path / "out" / "logs" / rows["engine"]["file"]).read_text() == "this job\n"


@pytest.fixture
def managed_host_job(host_case, tmp_path, monkeypatch):
    from dataclasses import replace

    from astrid.core.execution import generic_host

    case = host_case
    manifest = case.host.capabilities["test.echo"].manifest_path
    definition = json.loads(manifest.read_text())
    definition["id"] = "vibecomfy.run"
    manifest.write_text(json.dumps(definition))
    case.host.discover()
    case.host.capabilities["vibecomfy.run"] = replace(case.host.capabilities["vibecomfy.run"], ready=True)
    monkeypatch.setattr(case.host, "preflight", lambda *args: None)
    case.task["task"]["capability"] = "vibecomfy.run"
    root = tmp_path / "managed-engine"
    log = Path(engine_custody.engine_log_path(str(root), "comfyui"))
    log.parent.mkdir(parents=True)
    log.write_text("engine output\n")

    class Preparation:
        visibility = None
        recipe = SimpleNamespace(engine="comfyui", start=SimpleNamespace(port=1))

        def __init__(self, **kwargs):
            self.root = str(root)

        def close(self, **kwargs):
            pass

    monkeypatch.setattr(managed_preparation, "ManagedEnginePreparation", Preparation)
    monkeypatch.setattr(JobEvidence, "_vram", lambda *args: None)
    original = case.host._run_task_impl

    def run_task_impl(*args, **kwargs):
        case.terminal = kwargs["_terminal"]
        return original(*args, **kwargs)

    monkeypatch.setattr(case.host, "_run_task_impl", run_task_impl)
    case.log = log
    case.generic_host = generic_host
    return case


def test_managed_host_retains_engine_log_when_evidence_construction_fails(managed_host_job, monkeypatch):
    case = managed_host_job
    error = ValueError("original preparation failure")

    def unavailable(*args, **kwargs):
        raise ValueError("evidence construction failed")

    def reject(*args, **kwargs):
        raise error

    monkeypatch.setattr("astrid.core.generation.job_evidence.JobEvidence", unavailable)
    monkeypatch.setattr(case.generic_host, "_prepare_vibecomfy_execution_identity", reject)
    with pytest.raises(ValueError) as caught:
        case.host.run_task(case.task, lease_token="lease-result")
    assert caught.value is error
    declarations = {decl.name: source for decl, source in run_logs.read_declared(case.root / ".host-logs")}
    assert declarations["engine"] == case.log


@pytest.mark.parametrize("branch", ["early", "failed-evidence"])
def test_failed_task_survives_log_upload_failure(managed_host_job, monkeypatch, branch):
    from astrid.core.contracts.errors import AstridError

    case = managed_host_job
    error = AstridError("original preparation failure", code="preparation_failed")
    uploads = []

    def reject(*args, **kwargs):
        if branch == "failed-evidence":
            case.terminal["output_root"] = case.root / "outputs"
        raise error

    def broken_evidence(*args, **kwargs):
        raise ValueError("harvest failed")

    def upload(*args, **kwargs):
        uploads.append(True)
        raise OSError("log upload failed")

    monkeypatch.setattr(case.generic_host, "_prepare_vibecomfy_execution_identity", reject)
    monkeypatch.setattr(case.host, "_prepare_producer_evidence", broken_evidence)
    monkeypatch.setattr(case.host, "_upload_outputs", upload)
    with pytest.raises(AstridError) as caught:
        case.host.run_task(case.task, lease_token="lease-result")
    assert caught.value is error
    assert uploads == [True]
    assert len(case.runtime.failures) == 1
    failure = case.runtime.failures[0][3]
    assert failure["outputs"] == []
    expected = {"status": "failed", "host_failure": {"code": "preparation_failed", "message": str(error)}}
    if branch == "failed-evidence":
        expected["outcomes"] = {"execution": {"status": "failed"}}
    timing = failure["result"]["timing"]
    assert timing["schema"] == "astrid.generation.timing.v2"
    assert "cli" not in timing
    host = timing["host"]
    assert host["total"]["seconds"] >= 0
    assert sum(row["seconds"] or 0 for row in host["phases"].values()) + host["unaccounted"]["raw_seconds"] == pytest.approx(host["total"]["seconds"])
    assert json.loads((case.root / "result.json").read_text())["timing"] == timing
    expected["timing"] = timing
    assert failure["result"] == expected
    receipt = json.loads((case.root / "result.json").read_text())
    assert receipt["status"] == "failed"
    assert receipt["error"]["code"] == "preparation_failed"
    assert any("logs unavailable: OSError" in warning for warning in receipt["warnings"])


def test_hung_stats_are_bounded_when_finishing_job(tmp_path):
    release = threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            release.wait(15)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    preparation = SimpleNamespace(root=str(tmp_path / "engine"), recipe=SimpleNamespace(
        engine="comfyui", start=SimpleNamespace(port=server.server_port)), verification_seconds=0.0)
    log = Path(engine_custody.engine_log_path(preparation.root, "comfyui"))
    log.parent.mkdir(parents=True)
    log.write_text("job output\n")
    out = tmp_path / "out"
    try:
        started = time.monotonic()
        evidence = JobEvidence(out, preparation)
        begin_elapsed = time.monotonic() - started
        started = time.monotonic()
        evidence.finish()
        elapsed = time.monotonic() - started
        assert elapsed < 3.0
        assert begin_elapsed < 3.0
        record = json.loads((out / "job-run.json").read_text())
        assert record["vram_free_before_bytes"] is None
        assert record["vram_free_after_bytes"] is None
        assert any("VRAM after unavailable" in note for note in record["notes"])
        assert requests == ["/system_stats", "/system_stats"]
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


PHASE_KEYS = {"verify_models", "engine_start_or_reuse", "queue_wait", "execute", "collect"}


def _assert_phase_record(record):
    from datetime import datetime, timedelta

    assert set(record["phases"]) == PHASE_KEYS
    assert all(value is None or isinstance(value, (int, float)) and value >= 0
               for value in record["phases"].values())
    began, ended = (datetime.fromisoformat(record[key]) for key in ("started_at", "ended_at"))
    assert began.utcoffset() == ended.utcoffset() == timedelta(0)
    assert began <= ended
    assert record["phases"]["verify_models"] == record["model_verification_seconds"]


def test_consecutive_jobs_record_phase_seconds_and_supervisor_reuse(tmp_path, monkeypatch, job_harness):
    state, run, evidence, _ = job_harness
    ticks = iter([10.0, 12.0, 20.0, 20.125])
    monkeypatch.setattr(managed_preparation, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    with _FakeComfy() as fake:
        state.update(endpoint=fake.url, line="cold")
        run(tmp_path / "cold")
        state["line"] = "warm"
        run(tmp_path / "warm")
        _, cold = evidence(tmp_path / "cold")
        _, warm = evidence(tmp_path / "warm")
    for record in (cold, warm):
        _assert_phase_record(record)
        assert all(value is not None for value in record["phases"].values())
    assert cold["engine_reused"] is False
    assert warm["engine_reused"] is True
    assert cold["phases"]["engine_start_or_reuse"] == 2.0
    assert warm["phases"]["engine_start_or_reuse"] == 0.125


@pytest.mark.parametrize("failure", ["prepare", "execute"])
def test_failed_jobs_record_reached_phases(tmp_path, monkeypatch, job_harness, failure):
    state, run, evidence, _ = job_harness
    ticks = iter([10.0, 12.0])
    monkeypatch.setattr(managed_preparation, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    with _FakeComfy() as fake:
        state.update(endpoint=fake.url, line="failed phase", fail=failure == "execute",
                     prepare_fail=failure == "prepare")
        with pytest.raises(ValueError, match="preparation failed" if failure == "prepare" else "job failed"):
            run(tmp_path / "out")
        _, record = evidence(tmp_path / "out")
    _assert_phase_record(record)
    assert record["phases"]["collect"] >= 0
    if failure == "prepare":
        assert record["phases"]["engine_start_or_reuse"] == 0
        assert record["phases"]["execute"] == 0
        assert any("execute" in note and "not reached" in note for note in record["notes"])
    else:
        assert record["phases"]["engine_start_or_reuse"] == 2.0
        assert record["phases"]["execute"] >= 0


def test_supervisor_start_timing_accumulates_doctor_and_final_probe(monkeypatch):
    prep = object.__new__(managed_preparation.ManagedEnginePreparation)
    ticks = iter([10.0, 12.0, 13.0, 13.125])
    monkeypatch.setattr(managed_preparation, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    actions = iter([("executed", {}), ("skipped", {})])
    runner = SimpleNamespace(start=lambda **kwargs: next(actions))
    assert prep._start_engine(runner) == ("executed", {})
    assert prep._start_engine(runner) == ("skipped", {})
    assert prep.engine_start_seconds == 2.125
    assert prep.engine_reused is False


def test_unmeasurable_start_is_null_with_reason_and_preserves_result(tmp_path, monkeypatch):
    prep = object.__new__(managed_preparation.ManagedEnginePreparation)
    prep.root = str(tmp_path / "engine")
    prep.recipe = SimpleNamespace(engine="comfyui", start=SimpleNamespace(port=1))

    def clock_failed():
        raise OSError("clock unavailable")

    monkeypatch.setattr(managed_preparation, "time", SimpleNamespace(monotonic=clock_failed))
    runner = SimpleNamespace(start=lambda **kwargs: ("skipped", {"owned": True}))
    assert prep._start_engine(runner) == ("skipped", {"owned": True})
    monkeypatch.setattr(JobEvidence, "_vram", lambda *args: None)
    out = tmp_path / "out"
    evidence = JobEvidence(out, prep)
    evidence.finish()
    record = json.loads((out / "job-run.json").read_text())
    assert record["phases"]["engine_start_or_reuse"] is None
    assert any("engine_start_or_reuse unavailable" in note for note in record["notes"])
    assert record["engine_reused"] is True


def test_interrupted_queue_wait_is_null_with_reason(tmp_path, monkeypatch):
    prep = SimpleNamespace(root=str(tmp_path / "engine"), recipe=SimpleNamespace(
        engine="comfyui", start=SimpleNamespace(port=1)), verification_seconds=0.0)
    monkeypatch.setattr(JobEvidence, "_vram", lambda *args: None)
    out = tmp_path / "out"
    evidence = JobEvidence(out, prep)
    evidence.waiting()
    evidence.finish()
    record = json.loads((out / "job-run.json").read_text())
    assert record["phases"]["queue_wait"] is None
    assert any("queue_wait" in note and "interrupted" in note for note in record["notes"])


def test_engine_phase_includes_readiness_health_time(monkeypatch):
    prep = object.__new__(managed_preparation.ManagedEnginePreparation)
    ticks = iter([10.0, 12.0, 13.0, 16.0])
    monkeypatch.setattr(managed_preparation, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    runner = SimpleNamespace(start=lambda **kwargs: ("executed", {}), check_health=lambda: ("skipped", {"healthy": True}))
    prep._start_engine(runner)
    assert prep._time_engine(runner.check_health) == ("skipped", {"healthy": True})
    assert prep.engine_start_seconds == 5.0
    assert prep.engine_reused is False


def test_job_run_intervals_use_only_the_pods_monotonic_clock(tmp_path, monkeypatch):
    import astrid.core.generation.job_evidence as module
    now = [10.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(JobEvidence, "_vram", lambda *args: None)
    preparation = SimpleNamespace(root=str(tmp_path / "engine"), recipe=SimpleNamespace(
        engine="comfyui", start=SimpleNamespace(port=1)), engine_reused=True)
    evidence = JobEvidence(tmp_path / "out", preparation)
    evidence.waiting()
    now[0] = 12.0
    evidence.begin()
    evidence.start_execution()
    now[0] = 15.0
    evidence.start_collect()
    now[0] = 16.0
    evidence.finish()
    record = json.loads((evidence.out / "job-run.json").read_text())
    assert record["phases"]["queue_wait"] == 2.0
    assert record["phases"]["execute"] == 3.0
    assert record["phases"]["collect"] == 1.0
    assert record["engine_reused"] is True


def test_job_evidence_stamp_failure_keeps_the_run_record(tmp_path, monkeypatch):
    monkeypatch.setattr(JobEvidence, "_vram", lambda *args: None)
    preparation = SimpleNamespace(root=str(tmp_path / "engine"), recipe=SimpleNamespace(
        engine="comfyui", start=SimpleNamespace(port=1)))
    evidence = JobEvidence(tmp_path / "out", preparation)

    def broken(*args, **kwargs):
        raise OSError("clock unavailable")

    monkeypatch.setattr(evidence.timing, "stamp", broken)
    evidence.waiting()
    evidence.start_execution()
    evidence.start_collect()
    evidence.finish()
    assert json.loads((evidence.out / "job-run.json").read_text())["schema"] == "astrid.job-run.v1"
