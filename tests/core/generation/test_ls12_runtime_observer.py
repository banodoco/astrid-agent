"""LS-12 must attach the accepted meter's Runtime observer before entry."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tests import _local_speed_runtime
from tests.integrations import test_ls12_first_job as first_job


@pytest.mark.parametrize("caller", ["resident_sdk", "fresh_python_cli"])
def test_first_job_counter_attaches_runtime_before_caller_entry(
    caller, tmp_path, monkeypatch, request,
):
    meter = first_job.AttachedMeter()
    monkeypatch.setattr(first_job, "AttachedMeter", lambda: meter)
    monkeypatch.setenv("ASTRID_LS12_OBSERVE", "1")
    monkeypatch.setattr(first_job, "_workflow_file", lambda: tmp_path / "workflow.py")
    monkeypatch.setattr(first_job, "sources", lambda: ("", ""))
    process = SimpleNamespace(start=lambda: None, stop=lambda: None,
                              snapshot=lambda *args: {})
    monkeypatch.setattr(first_job, "ProcessObserver", lambda *args, **kwargs: process)
    monkeypatch.setattr(first_job, "ProcessReportCapture", lambda *args, **kwargs:
                        SimpleNamespace(install=lambda patch: None))
    traces = []
    daemon = SimpleNamespace(
        endpoint="http://127.0.0.1:1", credential_path=tmp_path / "credential",
        service=SimpleNamespace(realm={"id": "realm"}, store=SimpleNamespace(
            conn=SimpleNamespace(set_trace_callback=traces.append))),
    )
    observations = []
    mode = {"journal_mode": "wal", "synchronous": 2, "wal_autocheckpoint": 1000}

    class RuntimeObserver:
        def __init__(self, observed_daemon, observed_process):
            assert observed_daemon is daemon
            assert observed_process is process

        def install(self):
            observations.append("installed")
            return self

        def sqlite_mode(self):
            observations.append("sqlite_mode")
            return dict(mode)

    monkeypatch.setattr(_local_speed_runtime, "RuntimeObserver", RuntimeObserver)

    class CallerEntered(Exception):
        pass

    def enter_caller(*args, **kwargs):
        observations.append("caller")
        raise CallerEntered

    monkeypatch.setattr(first_job.subprocess, "run", enter_caller)

    @contextmanager
    def drive(*args, runtime_observer, call, **kwargs):
        runtime_observer(daemon)
        facade = SimpleNamespace(workflow=enter_caller)
        call(facade, {"placement": "attached", "project": "project"}, tmp_path)
        pytest.fail("caller entry sentinel was swallowed")
        yield

    monkeypatch.setattr(first_job, "_drive", drive)
    with pytest.raises(CallerEntered):
        first_job.test_first_attached_job_observation(
            caller, 0, tmp_path, monkeypatch, None, None, request,
        )
    assert observations == ["installed", "sqlite_mode", "caller", "sqlite_mode"]
    assert meter.sqlite_before == meter.sqlite_after == mode
    assert len(traces) == 1
