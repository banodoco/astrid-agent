"""Plan 32 T5c: which machine ran a job, on the attempt and in the task list."""
from __future__ import annotations

import socket

from astrid.core.cli.list_narrow import machine_label, render_list_table
from astrid.core.execution.generic_host import _local_machine


def test_a_local_job_records_local_and_its_device(monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1")
    assert _local_machine({}) == {"placement": "local", "machine": "local", "id": socket.gethostname(),
                                  "lease": None, "device": "1"}
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES")
    assert _local_machine({})["device"] == "auto"
    attached = _local_machine({"comfy_url": "http://127.0.0.1:8188"})
    assert (attached["placement"], attached["id"]) == ("attached", "http://127.0.0.1:8188")


def test_the_task_list_shows_a_machine_column_for_local_and_runpod_jobs():
    local = {"id": "a" * 32, "capability": "vibecomfy.run", "status": "succeeded",
             "result": {"machine": {"placement": "local", "machine": "local", "id": "studio", "device": "0"}}}
    remote = {"id": "b" * 32, "capability": "vibecomfy.run", "status": "succeeded",
              "result": {"machine": {"placement": "runpod", "machine": "runpod.runpod", "id": "pod123",
                                     "lease": "c" * 32, "device": "NVIDIA RTX 5090"}}}
    pending = {"id": "d" * 32, "capability": "vibecomfy.run", "status": "queued"}
    assert machine_label(local) == "local:studio (0)"
    assert machine_label(remote) == "runpod:pod123 (NVIDIA RTX 5090)"
    assert machine_label(pending) == "-"
    table = render_list_table([local, remote, pending], total=3, noun="tasks")
    header = table.splitlines()[0]
    assert "MACHINE" in header
    assert "local:studio (0)" in table and "runpod:pod123 (NVIDIA RTX 5090)" in table
