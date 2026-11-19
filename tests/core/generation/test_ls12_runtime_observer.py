"""LS-12 must attach the accepted meter's Runtime observer before entry."""
from contextlib import contextmanager
from io import BytesIO
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


@pytest.mark.parametrize("caller", ["resident_sdk", "fresh_python_cli"])
def test_first_job_megapixel_payload_reaches_standin_submission(
    caller, tmp_path, monkeypatch, request,
):
    """Exercise the real stand-in's media adapter before starting Runtime."""
    monkeypatch.delenv("ASTRID_LS12_OBSERVE", raising=False)
    monkeypatch.setattr(first_job, "_workflow_file", lambda: tmp_path / "workflow.py")
    pod = first_job.runpod_slot_pod
    # Graph validation has its own existing suite. This probe exercises the
    # unchanged PodComfy.submit output path with a prevalidated SaveImage node.
    monkeypatch.setattr(pod, "validate_prompt", lambda graph, info: {})
    standin = pod.PodComfy([], lambda: [])

    class StandinSubmitted(Exception):
        pass

    @contextmanager
    def drive(*args, **kwargs):
        status, receipt = standin.submit({"prompt": {
            "output": {"class_type": "SaveImage", "inputs": {}},
        }})
        assert status == 200
        output = standin.history[receipt["prompt_id"]]["outputs"]["output"]["images"][0]
        assert output["filename"].endswith(".png")
        payload, mime = standin.outputs[output["filename"]]
        assert mime == "image/png"
        assert len(payload) == 1048576
        assert payload == first_job.make_image(f"ls12:{caller}:0:False")
        with first_job.Image.open(BytesIO(payload)) as image:
            assert image.size == (1024, 1024)
            image.verify()
        raise StandinSubmitted
        yield

    monkeypatch.setattr(first_job, "_drive", drive)
    with pytest.raises(StandinSubmitted):
        first_job.test_first_attached_job_observation(
            caller, 0, tmp_path, monkeypatch, None, None, request,
        )
