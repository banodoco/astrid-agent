"""Observations of the existing attached path; this is not yet a budget gate.

The fixture Runtime and host run in this test process; CLI and pack workers
are separate. Timing and instrumentation runs are separate. Native flush
counts, when requested separately, are never inferred from SQLite commits.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections import Counter

import pytest
from PIL import Image

from astrid.core.generation.delivery import deliver_generation
from tests._local_speed_capture import ProcessReportCapture
from tests._local_speed_runtime import RuntimeObserver
from tests._local_speed_process import (
    MAX_REPORT_BYTES,
    PREFIX,
    ProcessObserver,
    read_reports,
    sources,
)
from tests.integrations.local_speed_sources import tuple_sources  # noqa: F401
from tests.integrations.test_runpod_slot_path_cpu import _drive
from tests.integrations.test_workflow_file_placements_cpu import INPUTS, _workflow_file

pytestmark = [pytest.mark.integration, pytest.mark.timeout(600),
              pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")]


def sample_indices():
    count = int(os.environ.get("ASTRID_LS2_SAMPLES", "1"))
    if not 1 <= count <= 20:
        raise ValueError("ASTRID_LS2_SAMPLES must be between 1 and 20")
    return range(count)


class AttachedMeter:
    """One monotonic clock for caller and stand-in HTTP events."""

    def __init__(self):
        self.lock = threading.Lock()
        self.events = []
        self.local_events = []
        self.timeline = []
        self.start = self.reply = None
        self.start_ns = self.reply_ns = None
        self.wall_start_ns = self.wall_reply_ns = None
        self.observer = None
        self.hashes_before = None
        self.parent_report = None
        self.runtime = None
        self.sqlite_before = self.sqlite_after = None

    def request(self, method, path, start, end):
        with self.lock:
            self.events.append((method, path, start, end))

    def comfy_timeline(self, prompt_id, queued, started, ended):
        with self.lock:
            self.timeline.append((prompt_id, queued, started, ended))

    def local_event(self, kind):
        with self.lock:
            self.local_events.append((kind, time.monotonic()))

    def begin(self):
        if self.observer is not None:
            self.sqlite_before = self.runtime.sqlite_mode()
            self.hashes_before = self.observer.snapshot()
        self.wall_start_ns = time.time_ns()
        self.start_ns = time.monotonic_ns()
        self.start = self.start_ns / 1e9

    def finish(self):
        self.reply_ns = time.monotonic_ns()
        self.wall_reply_ns = time.time_ns()
        self.reply = self.reply_ns / 1e9
        if self.observer is not None:
            self.parent_report = self.observer.snapshot(self.start_ns, self.reply_ns)
            self.sqlite_after = self.runtime.sqlite_mode()

    def call(self, facade, controls, workflow, destination):
        self.begin()
        try:
            result = facade.workflow(
                workflow=workflow, inputs=dict(INPUTS), engine="comfyui", **controls
            )
            assert result.ok, json.dumps(result.to_dict(), default=str)[:4000]
            assert deliver_generation(
                result, client=controls["client"], out=destination, project=controls["project"]
            )
            return result
        finally:
            self.finish()

    def report(self):
        with self.lock:
            events = [event for event in self.events if self.start <= event[2] <= self.reply]
            local = Counter(
                kind for kind, at in self.local_events if self.start <= at <= self.reply
            )
            timeline = [row for row in self.timeline if self.start <= row[1] <= self.reply]
        paths = Counter(f"{method} {path}" for method, path, _, _ in events)
        submissions = [
            (start, end)
            for method, path, start, end in events
            if (method, path) == ("POST", "/prompt")
        ]
        return {
            "schema": "astrid.local-speed-observation.v1",
            "gate_version": 3,
            "harness_settings": {"host_poll_seconds": "product_default", "machine_poll_seconds": "product_default",
                                 "sdk_poll_seconds": "product_default"},
            "caller": "resident_sdk",
            "caller_seconds": self.reply - self.start,
            "instrumented": self.observer is not None,
            "interval": {
                "pid": os.getpid(),
                "monotonic_start_ns": self.start_ns,
                "monotonic_reply_ns": self.reply_ns,
                "epoch_start_ns": self.wall_start_ns,
                "epoch_reply_ns": self.wall_reply_ns,
            },
            "standin_submit_seconds": [end - start for start, end in submissions],
            "standin_queue_seconds": [started - queued for _, queued, started, _ in timeline],
            "standin_execution_seconds": [ended - started for _, _, started, ended in timeline],
            "standin_timeline_scope": "synthetic synchronous fake; no real ComfyUI inference",
            "attached_http_paths": dict(sorted(paths.items())),
            "test_process_starts": None,
            "test_process_sleep_calls": None,
            "test_process_fsync_calls": None,
            "test_process_fdatasync_calls": None,
            "runtime_main_connection_commit_statements": local.get("sqlite_commit", 0)
            if self.observer is not None
            else None,
            "coverage": {
                "http": "stand-in socket and exact Runtime dispatch, method and normalized path",
                "process_starts": "see process_observations in counter runs; no monkeypatches in latency runs",
                "sleeps_and_polls": "Python counters only in observation runs; native trace remains separate",
                "compiles_and_emissions": None,
                "output_hash_passes": None,
                "forced_disk_writes": "native trace required; Python fsync counts are not native-flush proof",
                "sqlite_commits": "attempted COMMIT statements on Runtime main connection, including background activity; not successful commits or OS flushes",
                "local_counts": "all test-process threads in the caller interval, including background activity",
            },
            "budget_certified": False,
        }


@pytest.mark.parametrize("caller", ["resident_sdk", "fresh_python_cli"])
@pytest.mark.parametrize("sample_index", sample_indices())
def test_attached_caller_observation(
    caller, sample_index, tmp_path, monkeypatch, real_runtime, tuple_sources, request  # noqa: F811 - imported pytest fixture
):
    meter = AttachedMeter()
    connection = tmp_path / "prepared-connection.json"
    workflow = _workflow_file()
    warmup = []
    instrumented = os.environ.get("ASTRID_LS2_OBSERVE") == "1"
    if instrumented:
        captured_reports = ProcessReportCapture(PREFIX, max_report_bytes=MAX_REPORT_BYTES).install(
            monkeypatch
        )
        from tests.integrations.runpod_slot_pod import _media

        payload = _media("images")[0]
        h, process = sources()
        meter.observer = ProcessObserver(
            {hashlib.sha256(payload).hexdigest(): len(payload)},
            "parent_runtime_host_sdk",
            hash_source=h,
            observer_source=process,
        )
        meter.observer.start()
        request.addfinalizer(meter.observer.stop)

    def observe_runtime(daemon):
        connection.write_text(
            json.dumps(
                {
                    "endpoint": daemon.endpoint,
                    "credential_file": str(daemon.credential_path),
                    "realm_id": daemon.service.realm["id"],
                    "actor_id": "owner",
                }
            )
        )

        def trace(statement):
            if statement.strip().upper().startswith("COMMIT"):
                meter.local_event("sqlite_commit")

        if instrumented:
            daemon.service.store.conn.set_trace_callback(trace)
            meter.runtime = RuntimeObserver(daemon, meter.observer).install()

    def call(facade, controls, tmp):
        if caller == "resident_sdk":
            first = facade.workflow(
                workflow=workflow, inputs=dict(INPUTS), engine="comfyui", **controls
            )
            assert first.ok, json.dumps(first.to_dict(), default=str)[:4000]
            warmup.append(first)
            assert deliver_generation(
                first, client=controls["client"], out=tmp / "warmup", project=controls["project"]
            )
            return meter.call(facade, controls, workflow, tmp / "delivered")
        env = dict(os.environ, ASTRID_PREPARED_CONNECTION=str(connection))
        command = [
            sys.executable,
            "-m",
            "astrid",
            "generate",
            "workflow",
            str(workflow),
            "--prompt",
            INPUTS["prompt"],
            "--placement",
            controls["placement"],
            "--project",
            controls["project"],
            "--json",
        ]
        first = subprocess.run(
            [*command, "--out", str(tmp / "warmup")],
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert first.returncode == 0, first.stderr[-4000:] + first.stdout[-4000:]
        warmup.append(json.loads(first.stdout)["task_id"])
        meter.begin()
        try:
            return subprocess.run(
                [*command, "--out", str(tmp / "delivered")],
                env=env,
                capture_output=True,
                text=True,
                timeout=600,
            )
        finally:
            meter.finish()

    with _drive(
        ("image", "t2i"),
        tmp_path,
        monkeypatch,
        real_runtime,
        tuple_sources,
        call=call,
        target_kind="attached",
        request_observer=meter.request,
        timeline_observer=meter.comfy_timeline,
        runtime_observer=observe_runtime,
        product_defaults=True,
    ) as (client, project_id, provider, result):
        if caller == "resident_sdk":
            assert result.ok, json.dumps(result.to_dict(), default=str)[:4000]
            warmup_id, task_id = warmup[0].task_id, result.task_id
        else:
            assert result.returncode == 0, result.stderr[-4000:] + result.stdout[-4000:]
            warmup_id, task_id = warmup[0], json.loads(result.stdout)["task_id"]
        assert warmup_id != task_id
        output_proofs = []
        for directory in (tmp_path / "warmup", tmp_path / "delivered"):
            images = list(directory.rglob("*.png"))
            assert images
            for path in images:
                with Image.open(path) as image:
                    dimensions = list(image.size)
                    image.verify()
                payload = path.read_bytes()
                output_proofs.append(
                    {
                        "phase": directory.name,
                        "filename": path.name,
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "dimensions": dimensions,
                    }
                )
        for recorded_id in (warmup_id, task_id):
            task = client.tasks.show(recorded_id)
            assert task.ok and task.data["state"] == "succeeded"
        assert len(provider.comfy.prompts) == 2 and not provider.comfy.rejections
        assert len(provider.comfy.history) == 2
        assert len(set(provider.comfy.views)) == 2
        report = meter.report()
        report["caller"] = caller
        report["sample_index"] = sample_index
        report["job"] = "second invocation after successful warm-up in same host session"
        report["proof"] = {
            "warmup_task_id": warmup_id,
            "measured_task_id": task_id,
            "verified_states": ["succeeded", "succeeded"],
            "outputs": output_proofs,
        }
        report["caller_boundary"] = (
            "facade entry through result and caller-local delivery"
            if caller == "resident_sdk"
            else "process launch through exit, including CLI delivery"
        )
        if instrumented:
            # Successful attempts may delete their diagnostic files before
            # delivery returns. Capture at the existing drain, without keeping
            # attempts alive or broadening child permissions.
            texts, capture_gaps = captured_reports.snapshot()
            if caller == "fresh_python_cli":
                texts.append(result.stderr)
            children, gaps = read_reports(texts)
            gaps.extend(capture_gaps)
            children = [
                row
                for row in children
                if (meter.start_ns <= row.get("start_ns", -1) <= meter.reply_ns)
                or row.get("interval") == {"monotonic_start_ns": meter.start_ns,
                                            "monotonic_reply_ns": meter.reply_ns}
            ]
            rows = [meter.parent_report, *children]
            starts = [
                item
                for row in rows
                for item in row.get("process_starts", [])
                if meter.start_ns <= item["at_ns"] <= meter.reply_ns
            ]
            expected = {
                row["child_pid"] for row in starts if row["child_role"] in {"pack_worker", "cli"}
            }
            observed = {row["pid"] for row in children if "counts" in row}
            if expected - observed:
                gaps.append(
                    "missing process reports: " + ",".join(map(str, sorted(expected - observed)))
                )
            for row in children:
                if row.get("end_ns", 2**64) > meter.reply_ns and row.get("interval") != {
                    "monotonic_start_ns": meter.start_ns, "monotonic_reply_ns": meter.reply_ns,
                }:
                    gaps.append("child observation extends past reply")
            for row in rows:
                gaps.extend(row.get("coverage_gaps", []))
            report["process_observations"] = rows
            start_counts = Counter(item["child_role"] for item in starts)
            report["process_start_counts"] = {role: start_counts[role]
                for role in {"pack_worker", "cli", "host", "runtime", "external"}}
            report["runtime_process_model"] = (
                "Runtime and host are test-process threads, not hidden child processes"
            )
            report["python_counts"] = dict(
                sum((Counter(row.get("counts", {})) for row in rows), Counter())
            )
            report["wait_coverage"] = sorted(set.intersection(*(set(row.get("wait_coverage", [])) for row in rows)))
            report["wait_carry_in"] = dict(sum((Counter(row.get("wait_carry_in", {})) for row in rows), Counter()))
            report["runtime_http_paths"] = meter.runtime.report(meter.start_ns, meter.reply_ns)
            report["sqlite"] = {"before": meter.sqlite_before, "after": meter.sqlite_after,
                                "database": meter.runtime.database}
            report["write_spans"] = [dict(span, pid=row["pid"]) for row in rows for span in row.get("write_spans", [])]
            report["write_spans"].extend(meter.runtime.write_context_spans(
                meter.start_ns, meter.reply_ns, meter.wall_reply_ns))
            report["output_hash_streams"] = sum(row.get("output_hash_streams", 0) for row in rows)
            report["output_hash_bytes"] = sum(row.get("output_hash_bytes", 0) for row in rows)
            report["native_flushes"] = None
            report["observer_gaps"] = sorted(set(gaps))
            report["coverage"]["compiles_and_emissions"] = (
                "workflow compile, bundle compile and bundle emission entrypoints, by Python code identity"
            )
            report["coverage"]["output_hash_passes"] = (
                "completed SHA-256 streams for exact fixture output digest/size; Python constructors and file_digest, not arbitrary native hashing"
            )
            assert not gaps, json.dumps(report, sort_keys=True)
            assert report["output_hash_streams"] > 0, "output hashing was not observed"
        else:
            report["native_flushes"] = None
            report["observer_gaps"] = ["counter collection disabled in latency run"]
        assert report["caller_seconds"] > 0
        assert report["attached_http_paths"].get("POST /prompt") == 1
        assert len(report["standin_submit_seconds"]) == 1
        assert len(report["standin_queue_seconds"]) == len(report["standin_execution_seconds"]) == 1
        print("\nLOCAL_SPEED_OBSERVATION " + json.dumps(report, sort_keys=True))
