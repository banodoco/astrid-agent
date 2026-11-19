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
# Byte-identical to dev 9a7ae543 releases/14/evidence/ls1/image_fixture.py
# (Git blob 8f8b3ce67e9b4a7f3a6afa2a816385661f4487af).
from tests.integrations.ls12_image_fixture import make_image
from tests.integrations import runpod_slot_pod
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
    if os.environ.get("ASTRID_LS12_PHASES") == "1":
        from tests.ls12_phases import capture_phases
        report["phase_observations"] = capture_phases(meter)
        # Preserve partial diagnostics before the original strict counter
        # assertions. An incomplete report never becomes a passing sample.
        print("\nLS12_PARTIAL_PHASE_OBSERVATION " + json.dumps(report, sort_keys=True))
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
    phases = os.environ.get("ASTRID_LS12_PHASES") == "1"
    if phases and not instrumented:
        raise ValueError("phase observation requires the separate counter invocation")
    # Keep the standing LS-2 fixture unchanged. This first-job cell uses the
    # owner's 1024x1024, 1 MiB PNG for the complete caller-local delivery path.
    expected_payload = make_image(f"ls12:{caller}:{sample_index}:{instrumented}")
    original_media = runpod_slot_pod._media
    monkeypatch.setattr(runpod_slot_pod, "_media", lambda kind:
                        (expected_payload, "png", "image/png") if kind == "images" else original_media(kind))
    if instrumented:
        capture = ProcessReportCapture(PREFIX, max_report_bytes=MAX_REPORT_BYTES).install(monkeypatch)
        hashes, observer = sources()
        from tests.ls12_phases import PHASE_SITES
        meter.observer = ProcessObserver(
            {hashlib.sha256(expected_payload).hexdigest(): len(expected_payload)}, "parent_runtime_host_sdk",
            hash_source=hashes, observer_source=observer,
            **({"phase_sites": PHASE_SITES} if phases else {}),
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
            from tests._local_speed_runtime import RuntimeObserver

            def trace(statement):
                if statement.strip().upper().startswith("COMMIT"):
                    context = meter.observer.runtime_write_context(sys._getframe(1))
                    event = meter.observer.event("runtime_commit_statement", write_context=context)
                    meter.local_event("sqlite_commit", at_ns=event["at_ns"] if event is not None else None)
                    if context == "unattributed_runtime":
                        meter.observer.event("runtime_commit_statement_unattributed")
            daemon.service.store.conn.set_trace_callback(trace)
            meter.runtime = RuntimeObserver(daemon, meter.observer).install()

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
            assert dimensions == [1024, 1024]
            assert len(payload) == 1048576
            assert payload == expected_payload, "caller-local image differs from the owner's D1 payload"
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
