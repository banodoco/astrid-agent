"""Join the controller's shipped machine job to the real pod-side local runner."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.core.generation.local_job import run_job
from astrid.packs.runpod import machine
from tests.integrations.test_runpod_host_endpoint_r5 import tuple_sources  # noqa: F401
from tests.integrations.test_runpod_slot_path_cpu import (
    SlotPod, _capability_call, _drive, _managed_local_engine,
)

pytestmark = pytest.mark.integration


def test_controller_shipped_default_job_runs_on_the_real_local_path(
        tmp_path, monkeypatch, real_runtime, tuple_sources):
    """Transport the controller's exact bytes; only the engine's HTTP peer is fake.

    The existing provider journeys manufacture pod outputs. This separate seam
    replays their actual uploaded job and input files through unmodified run_job,
    so the controller and pod cannot silently disagree about the job contract.
    """
    monkeypatch.setattr(machine, "RUNS_ROOT", str(tmp_path / "pod" / "runs"))
    shipped = {}
    start = SlotPod._start_generation

    def capture(provider, directory, command):
        start(provider, directory, command)
        root = provider.detached[directory]["root"]
        shipped.update({name: data for name, data in provider.files.items()
                        if name.startswith(root + "/")})
        shipped["root"] = root

    monkeypatch.setattr(SlotPod, "_start_generation", capture)
    slot = ("image", "t2i")
    def call(facade, controls, tmp):
        return _capability_call(slot)(facade, {**controls, "align": "trust"}, tmp)

    with _drive(slot, tmp_path, monkeypatch, real_runtime, tuple_sources,
                target_kind="runpod", call=call) as (_, _, provider, result):
        assert result.ok, result.to_dict()

    root = Path(shipped.pop("root"))
    for name, data in shipped.items():
        path = Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    job_path = root / "job.json"
    # No reconstruction of the payload or rewriting of its remote input paths.
    assert job_path.read_bytes() == shipped[str(job_path)]
    job = json.loads(job_path.read_bytes())
    assert job["entry"] == provider.generations[0]["entry"]
    assert job["align"] == "trust"
    assert "A red cube on a white tabletop." in job["inputs"].values()
    assert {"python", "companion", "source", "workflow_inputs"} <= job["execution"].keys()
    assert all(Path(job["execution"][name]).is_file() for name in ("python", "companion", "source"))

    with _managed_local_engine(provider, tmp_path / "pod-engine", monkeypatch):
        manifest = run_job(job["entry"], job_path, root / "real-out", mode=job["mode"])
    assert manifest["managed_generation_result_path"]
    assert (root / "real-out" / manifest["managed_generation_result_path"]).is_file()
    assert provider.comfy.prompts and not provider.comfy.rejections
    assert provider.comfy.views, "the real runner never retrieved an engine output"
