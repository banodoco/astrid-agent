"""LS-12: the first attached job, using the standing LS-2 meter unchanged.

The Runtime and registered host are ready when the caller enters; no generation
warmup runs. Latency and counter observations use separate pytest invocations.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
import subprocess
import sys

from PIL import Image
import pytest

from tests._local_speed_capture import ProcessReportCapture
from tests._local_speed_process import (
    MAX_REPORT_BYTES, PREFIX, ProcessObserver, read_reports, sources,
)
from tests.integrations.local_speed_sources import tuple_sources  # noqa: F401
from tests.integrations.test_local_speed_meter import AttachedMeter
from tests.integrations.test_runpod_slot_path_cpu import _drive
from tests.integrations.test_workflow_file_placements_cpu import INPUTS, _workflow_file

pytestmark = [pytest.mark.integration, pytest.mark.timeout(600)]


def _sample_indices():
    count = int(os.environ.get("ASTRID_LS12_SAMPLES", "1"))
    if not 1 <= count <= 20:
        raise ValueError("ASTRID_LS12_SAMPLES must be between 1 and 20")
    return range(count)


def _counter_report(meter, capture, result, caller):
    texts, gaps = capture.snapshot()
    if caller == "fresh_python_cli":
        texts.append(result.stderr)
    children, read_gaps = read_reports(texts)
    gaps.extend(read_gaps)
    children = [row for row in children
                if meter.start_ns <= row.get("start_ns", -1) <= meter.reply_ns]
    rows = [meter.parent_report, *children]
    starts = [item for row in rows for item in row.get("process_starts", [])
              if meter.start_ns <= item["at_ns"] <= meter.reply_ns]
    expected = {row["child_pid"] for row in starts
                if row["child_role"] in {"pack_worker", "cli"}}
    observed = {row["pid"] for row in children if "counts" in row}
    if expected - observed:
        gaps.append("missing process reports: " + ",".join(map(str, sorted(expected - observed))))
    for row in children:
        if row.get("end_ns", 2**64) > meter.reply_ns:
            gaps.append("child observation extends past reply")
    for row in rows:
        gaps.extend(row.get("coverage_gaps", []))
    report = {
        "process_observations": rows,
        "process_start_counts": dict(Counter(row["child_role"] for row in starts)),
        "python_counts": dict(sum((Counter(row.get("counts", {})) for row in rows), Counter())),
        "output_hash_streams": sum(row.get("output_hash_streams", 0) for row in rows),
        "output_hash_bytes": sum(row.get("output_hash_bytes", 0) for row in rows),
        "observer_gaps": sorted(set(gaps)),
    }
    assert not gaps, json.dumps(report, sort_keys=True)
    assert report["output_hash_streams"] > 0, "output hashing was not observed"
    assert report["process_start_counts"].get("pack_worker", 0) == 1
    assert report["process_start_counts"].get("cli", 0) == (caller == "fresh_python_cli")
    return report


@pytest.mark.parametrize("caller", ["resident_sdk", "fresh_python_cli"])
@pytest.mark.parametrize("sample_index", _sample_indices())
def test_first_attached_job_observation(
    caller, sample_index, tmp_path, monkeypatch, real_runtime, tuple_sources, request,
):
    meter = AttachedMeter()
    connection = tmp_path / "prepared-connection.json"
    workflow = _workflow_file()
    instrumented = os.environ.get("ASTRID_LS12_OBSERVE") == "1"
    if instrumented:
        capture = ProcessReportCapture(PREFIX, max_report_bytes=MAX_REPORT_BYTES).install(monkeypatch)
        from tests.integrations.runpod_slot_pod import _media
        payload = _media("images")[0]
        hashes, observer = sources()
        meter.observer = ProcessObserver(
            {hashlib.sha256(payload).hexdigest(): len(payload)}, "parent_runtime_host_sdk",
            hash_source=hashes, observer_source=observer,
        )
        meter.observer.start()
        request.addfinalizer(meter.observer.stop)

    def observe_runtime(daemon):
        connection.write_text(json.dumps({
            "endpoint": daemon.endpoint,
            "credential_file": str(daemon.credential_path),
            "realm_id": daemon.service.realm["id"], "actor_id": "owner",
        }), encoding="utf-8")
        if instrumented:
            def trace(statement):
                if statement.strip().upper().startswith("COMMIT"):
                    meter.local_event("sqlite_commit")
            daemon.service.store.conn.set_trace_callback(trace)

    def call(facade, controls, tmp):
        if caller == "resident_sdk":
            return meter.call(facade, controls, workflow, tmp / "delivered")
        command = [sys.executable, "-m", "astrid", "generate", "workflow", str(workflow),
                   "--prompt", INPUTS["prompt"], "--placement", controls["placement"],
                   "--project", controls["project"], "--json", "--out", str(tmp / "delivered")]
        meter.begin()
        try:
            return subprocess.run(command, env=dict(os.environ, ASTRID_PREPARED_CONNECTION=str(connection)),
                                  capture_output=True, text=True, timeout=600)
        finally:
            meter.finish()

    # LS-2.2 distinguishes the product cadence from the accelerated journey
    # fixture. Record the same controls that are passed to that fixture.
    drive_controls = {"product_defaults": True}
    with _drive(("image", "t2i"), tmp_path, monkeypatch, real_runtime, tuple_sources,
                call=call, target_kind="attached", request_observer=meter.request,
                timeline_observer=meter.comfy_timeline, runtime_observer=observe_runtime,
                **drive_controls,
                ) as (client, project_id, provider, result):
        if caller == "resident_sdk":
            assert result.ok, json.dumps(result.to_dict(), default=str)[:4000]
            task_id = result.task_id
        else:
            assert result.returncode == 0, result.stderr[-4000:] + result.stdout[-4000:]
            task_id = json.loads(result.stdout)["task_id"]
        task = client.tasks.show(task_id)
        assert task.ok and task.data["state"] == "succeeded"
        assert len(provider.comfy.prompts) == 1 and not provider.comfy.rejections
        assert len(provider.comfy.history) == 1
        assert len(set(provider.comfy.views)) == 1
        outputs = []
        images = list((tmp_path / "delivered").rglob("*.png"))
        assert images
        for path in images:
            with Image.open(path) as image:
                dimensions = list(image.size)
                image.verify()
            payload = path.read_bytes()
            outputs.append({"filename": path.name, "bytes": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(), "dimensions": dimensions})
        report = meter.report()
        report.update({
            "caller": caller, "sample_index": sample_index,
            "drive_controls": drive_controls,
            "job": "first invocation after host admission; no generation warmup",
            "proof": {"measured_task_id": task_id, "verified_states": ["succeeded"], "outputs": outputs},
            "caller_boundary": ("facade entry through result and caller-local delivery"
                                if caller == "resident_sdk" else "process launch through exit, including CLI delivery"),
            "runtime_process_model": "Runtime and host are test-process threads",
            "native_flushes": None,
        })
        if instrumented:
            report.update(_counter_report(meter, capture, result, caller))
        else:
            report["observer_gaps"] = ["counter collection disabled in latency run"]
        assert report["caller_seconds"] > 0
        assert report["attached_http_paths"].get("POST /prompt") == 1
        assert len(report["standin_submit_seconds"]) == 1
        assert len(report["standin_queue_seconds"]) == len(report["standin_execution_seconds"]) == 1
        report["overhead_seconds"] = report["caller_seconds"] - sum(report["standin_execution_seconds"])
        print("\nLS12_FIRST_JOB_OBSERVATION " + json.dumps(report, sort_keys=True))
