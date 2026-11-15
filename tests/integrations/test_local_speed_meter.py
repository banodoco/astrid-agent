"""Observations of the existing attached path; this is not yet a budget gate.

The Runtime and pack worker have their own interpreters. Counts collected here
are explicitly scoped; absence of cross-process instrumentation is not a zero.
"""
from __future__ import annotations

import json
import os
import threading
import time
import subprocess
import sys
from collections import Counter

import pytest
from PIL import Image

from astrid.core.generation.delivery import deliver_generation
from tests.integrations.test_runpod_host_endpoint_r5 import tuple_sources  # noqa: F401
from tests.integrations.test_runpod_slot_path_cpu import _drive
from tests.integrations.test_workflow_file_placements_cpu import INPUTS, _workflow_file

pytestmark = [pytest.mark.integration, pytest.mark.timeout(600)]


class AttachedMeter:
    """One monotonic clock for caller and stand-in HTTP events."""

    def __init__(self):
        self.lock = threading.Lock()
        self.events = []
        self.local_events = []
        self.timeline = []
        self.start = self.reply = None

    def request(self, method, path, start, end):
        with self.lock:
            self.events.append((method, path, start, end))

    def comfy_timeline(self, prompt_id, queued, started, ended):
        with self.lock:
            self.timeline.append((prompt_id, queued, started, ended))

    def local_event(self, kind):
        with self.lock:
            self.local_events.append((kind, time.monotonic()))

    def call(self, facade, controls, workflow):
        self.start = time.monotonic()
        try:
            return facade.workflow(workflow=workflow, inputs=dict(INPUTS), engine="comfyui", **controls)
        finally:
            self.reply = time.monotonic()

    def report(self):
        with self.lock:
            events = [event for event in self.events if self.start <= event[2] <= self.reply]
            local = Counter(kind for kind, at in self.local_events if self.start <= at <= self.reply)
            timeline = [row for row in self.timeline if self.start <= row[1] <= self.reply]
        paths = Counter(f"{method} {path}" for method, path, _, _ in events)
        submissions = [(start, end) for method, path, start, end in events
                       if (method, path) == ("POST", "/prompt")]
        return {
            "schema": "astrid.local-speed-observation.v1",
            "caller": "resident_sdk",
            "caller_seconds": self.reply - self.start,
            "standin_submit_seconds": [end - start for start, end in submissions],
            "standin_queue_seconds": [started - queued for _, queued, started, _ in timeline],
            "standin_execution_seconds": [ended - started for _, _, started, ended in timeline],
            "standin_timeline_scope": "synthetic synchronous fake; no real ComfyUI inference",
            "attached_http_paths": dict(sorted(paths.items())),
            "test_process_starts": local.get("process_start", 0),
            "test_process_sleep_calls": local.get("sleep", 0),
            "test_process_fsync_calls": local.get("fsync", 0),
            "test_process_fdatasync_calls": local.get("fdatasync", 0) if hasattr(os, "fdatasync") else None,
            "runtime_main_connection_commit_statements": local.get("sqlite_commit", 0),
            "coverage": {
                "http": "stand-in socket only; Runtime and other HTTP paths unobserved",
                "process_starts": "test process only; child and Runtime process starts unobserved",
                "sleeps_and_polls": "test process sleeps only; child and Runtime sleeps and polls unobserved",
                "compiles_and_emissions": None, "output_hash_passes": None,
                "forced_disk_writes": "Python fsync/fdatasync in test process only; SQLite and native Windows flushes unobserved",
                "sqlite_commits": "attempted COMMIT statements on Runtime main connection, including background activity; not successful commits or OS flushes",
                "local_counts": "all test-process threads in the caller interval, including background activity",
            },
            "budget_certified": False,
        }


@pytest.mark.parametrize("caller", ["resident_sdk", "fresh_python_cli"])
def test_attached_caller_observation(caller, tmp_path, monkeypatch, real_runtime, tuple_sources):
    meter = AttachedMeter()
    connection = tmp_path / "prepared-connection.json"
    workflow = _workflow_file()
    warmup = []
    popen_init, sleep = subprocess.Popen.__init__, time.sleep

    def observed_popen(process, *args, **kwargs):
        popen_init(process, *args, **kwargs)
        meter.local_event("process_start")

    def observed_sleep(seconds):
        meter.local_event("sleep")
        return sleep(seconds)

    def observe_sync(name):
        original = getattr(os, name)
        def sync(*args, **kwargs):
            result = original(*args, **kwargs)
            meter.local_event(name)
            return result
        monkeypatch.setattr(os, name, sync)

    def observe_runtime(daemon):
        connection.write_text(json.dumps({"endpoint": daemon.endpoint, "credential_file": str(daemon.credential_path),
                                          "realm_id": daemon.service.realm["id"], "actor_id": "owner"}))
        def trace(statement):
            if statement.strip().upper().startswith("COMMIT"):
                meter.local_event("sqlite_commit")
        daemon.service.store.conn.set_trace_callback(trace)

    for name in ("fsync", "fdatasync"):
        if hasattr(os, name):
            observe_sync(name)
    monkeypatch.setattr(subprocess.Popen, "__init__", observed_popen)
    monkeypatch.setattr(time, "sleep", observed_sleep)
    def call(facade, controls, tmp):
        if caller == "resident_sdk":
            first = facade.workflow(workflow=workflow, inputs=dict(INPUTS), engine="comfyui", **controls)
            assert first.ok, json.dumps(first.to_dict(), default=str)[:4000]
            warmup.append(first)
            return meter.call(facade, controls, workflow)
        env = dict(os.environ, ASTRID_PREPARED_CONNECTION=str(connection))
        command = [sys.executable, "-m", "astrid", "generate", "workflow", str(workflow),
                   "--prompt", INPUTS["prompt"], "--placement", controls["placement"],
                   "--project", controls["project"], "--json"]
        first = subprocess.run([*command, "--out", str(tmp / "warmup")], env=env,
                               capture_output=True, text=True, timeout=600)
        assert first.returncode == 0, first.stderr[-4000:] + first.stdout[-4000:]
        warmup.append(json.loads(first.stdout)["task_id"])
        meter.start = time.monotonic()
        try:
            return subprocess.run([*command, "--out", str(tmp / "delivered")], env=env,
                                  capture_output=True, text=True, timeout=600)
        finally:
            meter.reply = time.monotonic()

    with _drive(("image", "t2i"), tmp_path, monkeypatch, real_runtime, tuple_sources,
                call=call, target_kind="attached", request_observer=meter.request,
                timeline_observer=meter.comfy_timeline, runtime_observer=observe_runtime) as (client, project_id, provider, result):
        if caller == "resident_sdk":
            assert result.ok, json.dumps(result.to_dict(), default=str)[:4000]
            delivered = deliver_generation(result, client=client, out=tmp_path / "delivered", project=project_id)
            assert delivered
            assert deliver_generation(warmup[0], client=client, out=tmp_path / "warmup", project=project_id)
            warmup_id, task_id = warmup[0].task_id, result.task_id
        else:
            assert result.returncode == 0, result.stderr[-4000:] + result.stdout[-4000:]
            warmup_id, task_id = warmup[0], json.loads(result.stdout)["task_id"]
        assert warmup_id != task_id
        for directory in (tmp_path / "warmup", tmp_path / "delivered"):
            images = list(directory.rglob("*.png"))
            assert images
            for path in images:
                with Image.open(path) as image:
                    image.verify()
        for recorded_id in (warmup_id, task_id):
            task = client.tasks.show(recorded_id)
            assert task.ok and task.data["state"] == "succeeded"
        assert len(provider.comfy.prompts) == 2 and not provider.comfy.rejections
        assert len(provider.comfy.history) == 2
        assert len(set(provider.comfy.views)) == 2
        report = meter.report()
        report["caller"] = caller
        report["job"] = "second invocation after successful warm-up in same host session"
        report["caller_boundary"] = ("facade entry through result; delivery verified afterward" if caller == "resident_sdk"
                                     else "process launch through exit, including CLI delivery")
        assert report["caller_seconds"] > 0
        assert report["attached_http_paths"].get("POST /prompt") == 1
        assert len(report["standin_submit_seconds"]) == 1
        assert len(report["standin_queue_seconds"]) == len(report["standin_execution_seconds"]) == 1
        print("LOCAL_SPEED_OBSERVATION " + json.dumps(report, sort_keys=True))
