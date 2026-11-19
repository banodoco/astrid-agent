"""Phase diagnostics preserve real calls, child admission and incomplete proof."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import zlib

import pytest

from tests._local_speed_process import (
    MARKER, PREFIX, ProcessObserver, _decode_phase_spans, _encode_phase_spans, instrument_argv, read_reports, sources,
)
from tests.ls12_phases import PHASE_SITES, breakdown, duration, summarize

DATA = b"an observed output for phase accounting"
TARGET = {hashlib.sha256(DATA).hexdigest(): len(DATA)}


def observer(sites):
    hashes, process = sources()
    return ProcessObserver(TARGET, "phase_test", hash_source=hashes, observer_source=process, phase_sites=sites)


def test_nested_calls_exceptions_and_generator_suspension_keep_real_boundaries():
    namespace = {"__name__": "phase_fixture", "time": time}
    exec("def inner():\n    time.sleep(0.001)\n    raise LookupError('unchanged')\n"
         "def outer():\n    try: inner()\n    except LookupError: pass\n"
         "def suspended():\n    yield 1\n    yield 2\n", namespace)
    meter = observer({"phase_fixture:" + name: name for name in ("inner", "outer", "suspended")})
    meter.start()
    try:
        namespace["outer"]()
        stream = namespace["suspended"]()
        assert next(stream) == 1
        pause_start = time.perf_counter_ns()
        time.sleep(0.002)
        pause_end = time.perf_counter_ns()
        assert next(stream) == 2
        with pytest.raises(StopIteration):
            next(stream)
        report = meter.snapshot()
    finally:
        meter.stop()
    spans = report["phase_spans"]
    assert report["coverage_gaps"] == []
    outer = next(span for span in spans if span["phase"] == "outer")
    inner = next(span for span in spans if span["phase"] == "inner")
    assert outer["monotonic_start_ns"] < inner["monotonic_start_ns"] < inner["monotonic_end_ns"] < outer["monotonic_end_ns"]
    pieces = [span for span in spans if span["phase"] == "suspended"]
    assert len(pieces) == 3
    assert all(span["monotonic_end_ns"] <= pause_start or span["monotonic_start_ns"] >= pause_end for span in pieces)


def test_hash_durations_share_the_existing_digest_size_and_copy_identity():
    meter = observer({"phase_fixture:unused": "unused"})
    meter.start()
    try:
        digest = hashlib.sha256(DATA[:8])
        other = digest.copy()
        digest.update(DATA[8:])
        other.update(DATA[8:])
        digest.hexdigest()
        other.hexdigest()
        digest.copy().hexdigest()
        hashlib.sha256(b"unrelated").hexdigest()
        report = meter.snapshot()
    finally:
        meter.stop()
    assert report["output_hash_streams"] == 2
    assert report["output_hash_bytes"] == 2 * len(DATA) - 8
    assert len(report["output_hash_spans"]) == 3
    assert sum(span["bytes"] for span in report["output_hash_spans"]) == report["output_hash_bytes"]
    assert all(0 < span["monotonic_start_ns"] <= span["monotonic_end_ns"] for span in report["output_hash_spans"])


def test_optional_phases_survive_real_cli_child_transport_without_changing_exit(tmp_path):
    package = tmp_path / "astrid"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(
        "import hashlib\ndef phase():\n    return hashlib.sha256(" + repr(DATA) + ").hexdigest()\n"
        "print(phase())\nraise SystemExit(7)\n")
    meter = observer({"__main__:phase": "child_phase"})
    meter.start()
    try:
        child = subprocess.run([sys.executable, "-m", "astrid"], cwd=tmp_path,
                               env={**os.environ, "PYTHONPATH": ""}, capture_output=True, text=True, timeout=30)
        parent = meter.snapshot()
    finally:
        meter.stop()
    rows, gaps = read_reports([child.stderr])
    assert child.returncode == 7 and child.stdout.strip() in TARGET
    assert gaps == [] and len(rows) == 1
    assert rows[0]["coverage_gaps"] == [] and rows[0]["output_hash_streams"] == 1
    assert len(rows[0]["phase_spans"]) == len(rows[0]["output_hash_spans"]) == 1
    span = rows[0]["phase_spans"][0]
    assert span["phase"] == "child_phase" and span["monotonic_end_ns"] >= span["monotonic_start_ns"]
    assert parent["process_starts"][0]["spawn_start_ns"] <= rows[0]["start_ns"]


def test_full_phase_configuration_keeps_strict_startup_prefix_and_windows_command_bound():
    from astrid.core.execution.generic_host import _STRICT_NETWORK_STARTUP
    hashes, process = sources()
    original = [sys.executable, "-X", "utf8", "-I", "-S", "-c", _STRICT_NETWORK_STARTUP,
                json.dumps({"module": "astrid.core.execution.generic_host_worker"}), "--external-source-fd", "42"]
    gaps = set()
    actual = instrument_argv(original, hashes, process, TARGET, gaps, phase_sites=PHASE_SITES)
    assert actual[:6] == original[:6] and actual[7:] == original[7:]
    call = "runpy.run_module(config['module'], run_name='__main__', alter_sys=True)"
    assert actual[6].split(MARKER)[0] == _STRICT_NETWORK_STARTUP[:_STRICT_NETWORK_STARTUP.rfind(call)] + "\n"
    assert len(subprocess.list2cmdline(actual)) < 30000
    assert gaps == set()


@pytest.mark.parametrize("field", ["phase_spans", "output_hash_spans"])
def test_truncated_compressed_phase_evidence_is_rejected(field):
    raw = base64.b85encode(zlib.compress(b"[]")[:-2]).decode()
    row = {"schema": "astrid.local-speed-process.v1", "pid": 1, "start_ns": 1, field + "_zlib": raw}
    reports, gaps = read_reports([PREFIX + json.dumps(row)])
    assert reports == [] and gaps == ["malformed or truncated process report"]


def test_phase_compression_preserves_nested_open_and_task_bound_spans():
    spans = [{"site": "example", "phase": "phase", "tid": 1, "runtime_scope": 9,
              "task_id": "task", "monotonic_start_ns": 20, "monotonic_end_ns": 50},
             {"site": "example", "phase": "phase", "tid": 1, "runtime_scope": 9,
              "task_id": "task", "monotonic_start_ns": 25, "monotonic_end_ns": 30},
             {"site": "other", "phase": "other", "tid": 2, "runtime_scope": None,
              "monotonic_start_ns": 45, "monotonic_end_ns": None}]
    encoded = _encode_phase_spans(spans, 10)
    assert len(encoded["metadata"]) == 2
    assert _decode_phase_spans(json.loads(json.dumps(encoded))) == spans
    encoded["rows"][0][0] = 5
    with pytest.raises(ValueError, match="invalid phase delta"):
        _decode_phase_spans(encoded)


def test_phase_overflow_remains_an_explicit_gap(monkeypatch):
    import tests._local_speed_process as module
    namespace = {"__name__": "phase_fixture"}
    exec("def work(): pass", namespace)
    meter = observer({"phase_fixture:work": "work"})
    monkeypatch.setattr(module, "MAX_EVENTS", 1)
    meter.start()
    try:
        namespace["work"]()
        namespace["work"]()
        report = meter.snapshot()
    finally:
        meter.stop()
    assert "Python phase observation limit exceeded" in report["coverage_gaps"]


def test_runtime_return_identity_excludes_specs_and_empty_or_refused_claims():
    namespace = {"__name__": "runtime_protocol.service"}
    exec("class RuntimeService:\n"
         "    def create_task(self): return {'task': {'id': 'task'}, 'spec': 'DO-NOT-RETAIN'}\n"
         "    def claim_next(self, result): return result\n"
         "    def settle_attempt(self): return {'data': {'task_id': 'task'}, 'receipt': 'DO-NOT-RETAIN'}\n", namespace)
    service = namespace["RuntimeService"]()
    meter = observer(PHASE_SITES)
    meter.context.scope = 123
    meter.start()
    try:
        service.create_task()
        service.claim_next({'task_id': 'task', 'attempt_id': 'attempt', 'spec': 'DO-NOT-RETAIN'})
        service.claim_next(None)
        service.claim_next({'task': {'task_id': 'task'}, 'waiting_reason': 'not-a-lease'})
        service.settle_attempt()
        report = meter.snapshot()
    finally:
        meter.stop()
    spans = report["phase_spans"]
    assert len(spans) == 5 and all(span["runtime_scope"] == 123 for span in spans)
    assert [span.get("task_id") for span in spans] == ['task', 'task', None, None, 'task']
    assert 'DO-NOT-RETAIN' not in json.dumps(report)


def sample():
    def span(start, end, **extra):
        return {"monotonic_start_ns": start * 10**9, "monotonic_end_ns": end * 10**9, **extra}
    return {"caller": "resident_sdk", "sample_index": 0,
            "proof": {"measured_task_id": "measured-task"},
            "interval": {"monotonic_start_ns": 10 * 10**9, "monotonic_reply_ns": 30 * 10**9},
            "phase_observations": {"schema": "astrid.ls12-phases.v1", "phase_sites": PHASE_SITES,
                "standin_timeline": [{"queued_ns": 20 * 10**9, "start_ns": 21 * 10**9, "end_ns": 23 * 10**9}],
                "standin_http": [],
                "runtime_spans": [span(12, 13, route="POST /v1/tasks", scope=100), span(14, 15, route="POST /v1/tasks/claim", scope=101),
                                  span(25, 27, route="POST /v1/attempts/<attempt>/settle", scope=102)]},
            "process_observations": [
                {"pid": 1, "role": "parent_runtime_host_sdk", "start_ns": 1,
                 "process_starts": [{"child_pid": 2, "child_role": "pack_worker", "at_ns": 16 * 10**9,
                                     "spawn_start_ns": 16 * 10**9 - 1}],
                 "phase_spans": [span(18, 24, phase="workflow_compile", site="vibecomfy.workflow:VibeWorkflow.compile"),
                                 span(19, 20, phase="workflow_compile", site="vibecomfy.workflow:VibeWorkflow.compile"),
                                 span(12, 13, phase="runtime_admission", site="runtime_protocol.service:RuntimeService.create_task",
                                      task_id="measured-task", runtime_scope=100),
                                 span(14, 15, phase="runtime_claim", site="runtime_protocol.service:RuntimeService.claim_next",
                                      task_id="measured-task", runtime_scope=101),
                                 span(25, 27, phase="settlement", site="runtime_protocol.service:RuntimeService.settle_attempt",
                                      task_id="measured-task", runtime_scope=102)],
                 "output_hash_spans": []},
                {"pid": 2, "role": "pack_worker", "start_ns": 17 * 10**9,
                 "process_starts": [], "phase_spans": [], "output_hash_spans": []}]}


def test_union_clips_nested_spans_subtracts_only_execution_and_keeps_raw_evidence():
    row = sample()
    result = breakdown(row)
    assert result["instrumented_overhead_seconds"] == 18
    assert result["phases"]["workflow_compile"] == {"spans": 2, "wall_seconds": 6, "overhead_seconds": 4}
    assert result["phases"]["admission_to_claim"]["wall_seconds"] == 2
    assert result["phases"]["settlement_to_reply"]["wall_seconds"] == 3
    assert len([span for span in result["raw_spans"] if span["phase"] == "workflow_compile"]) == 2
    assert duration([(1, 3), (2, 4)], [(2, 3)]) == 2 / 1e9
    second = breakdown({**row, "sample_index": 1})
    second["phases"]["workflow_compile"]["overhead_seconds"] = 6
    assert summarize([result, second])["resident_sdk"]["phases"]["workflow_compile"] == {
        "samples": 2, "median": 5, "slowest": 6, "jobs_with_observed_spans": 2}


@pytest.mark.parametrize("missing", ["phase_observations", "worker_phase", "admission", "claim", "settlement", "task_identity"])
def test_missing_phase_evidence_is_not_zero(missing):
    row = sample()
    if missing == "phase_observations":
        row.pop(missing)
    elif missing == "worker_phase":
        row["process_observations"][1].pop("phase_spans")
    elif missing == "task_identity":
        row["proof"]["measured_task_id"] = "unrelated-task"
    else:
        index = {"admission": 0, "claim": 1, "settlement": 2}[missing]
        del row["phase_observations"]["runtime_spans"][index]
    with pytest.raises(ValueError):
        breakdown(row)
