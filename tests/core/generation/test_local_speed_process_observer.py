"""Calibration of Plan 55 observation, not product-speed evidence."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from tests._local_speed_process import (
    MARKER, PREFIX, ProcessObserver, classify_command, instrument_argv, read_reports, sources,
)

DATA = b"plan55-observed-output"
TARGETS = {hashlib.sha256(DATA).hexdigest(): len(DATA)}


def observer():
    h, p = sources()
    return ProcessObserver(TARGETS, "test_parent", hash_source=h, observer_source=p)


def test_actual_hash_file_digest_and_native_python_sync_are_counted(tmp_path):
    meter = observer()
    path = tmp_path / "output"
    path.write_bytes(DATA)
    meter.start()
    try:
        begin = time.perf_counter_ns()
        hashlib.sha256(DATA).digest()
        with path.open("rb") as handle:
            hashlib.file_digest(handle, "sha256").digest()
        with path.open("ab") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        time.sleep(0)
        report = meter.snapshot(begin)
    finally:
        meter.stop()
    assert report["output_hash_streams"] == 2
    assert report["output_hash_bytes"] == 2 * len(DATA)
    assert report["counts"]["fsync_attempt"] == report["counts"]["fsync_success"] == 1
    assert report["counts"]["sleep_success"] == 1
    assert report["native_flushes"] is None  # Python calls are not an OS/native trace.
    assert report["coverage_gaps"] == []


def test_compile_and_emission_boundaries_are_distinct():
    meter = observer()
    namespace = {}
    exec(compile("def compile(): pass", "/fixture/vibecomfy/workflow.py", "exec"), namespace)
    workflow_compile = namespace["compile"]
    exec(compile("def compile(): pass\ndef emit_bundle_with_candidate(): pass\ndef emit_bundle(): pass",
                 "/fixture/vibecomfy/workflow_bundle.py", "exec"), namespace)
    meter.start()
    try:
        workflow_compile()
        namespace["compile"]()
        namespace["emit_bundle_with_candidate"]()
        namespace["emit_bundle"]()
        report = meter.snapshot()
    finally:
        meter.stop()
    assert report["counts"]["workflow_compile"] == 1
    assert report["counts"]["bundle_compile"] == 1
    assert report["counts"]["bundle_emission"] == 2


def test_actual_spawn_is_counted_and_unrelated_argv_is_untouched():
    meter = observer()
    args = [sys.executable, "-c", "print('ordinary child')"]
    meter.start()
    try:
        child = subprocess.run(args, capture_output=True, text=True, timeout=30, check=True)
        report = meter.snapshot()
    finally:
        meter.stop()
    assert child.args == args
    assert child.stdout.strip() == "ordinary child"
    assert len(report["process_starts"]) == 1
    assert report["process_starts"][0]["child_role"] == "external"


def test_cli_observation_preserves_real_child_exit_output_and_arguments(tmp_path):
    # A deliberately synthetic module exercises the -m wrapper. This is not
    # the product CLI and does not claim product entrypoint proof.
    root = tmp_path / "astrid"
    root.mkdir()
    (root / "__init__.py").write_text("")
    (root / "__main__.py").write_text(
        "import hashlib,sys\nhashlib.file_digest(__import__('io').BytesIO(" + repr(DATA) + "),'sha256')\n"
        "print('synthetic',sys.argv[1:])\nraise SystemExit(7)\n")
    meter = observer()
    meter.start()
    try:
        child = subprocess.run([sys.executable, "-m", "astrid", "--value", "two words"],
            cwd=tmp_path, env={**os.environ, "PYTHONPATH": ""}, capture_output=True, text=True, timeout=30)
        parent = meter.snapshot()
    finally:
        meter.stop()
    reports, gaps = read_reports([child.stderr])
    assert child.returncode == 7
    assert child.stdout.strip() == "synthetic ['--value', 'two words']"
    assert gaps == [] and len(reports) == 1
    assert reports[0]["output_hash_streams"] == 1
    assert reports[0]["role"] == "cli"
    assert parent["process_starts"][0]["child_pid"] == reports[0]["pid"]
    assert parent["process_starts"][0]["child_role"] == "cli"


def test_strict_startup_remains_before_instrumentation():
    from astrid.core.execution.generic_host import _STRICT_NETWORK_STARTUP
    h, p = sources()
    argv = [sys.executable, "-X", "utf8", "-I", "-S", "-c", _STRICT_NETWORK_STARTUP,
            json.dumps({"module": "astrid.core.execution.generic_host_worker"}), "--external-source-fd", "42"]
    gaps = set()
    actual = instrument_argv(argv, h, p, TARGETS, gaps)
    assert actual[:6] == argv[:6] and actual[7:] == argv[7:]
    before, after = actual[6].split(MARKER, 1)
    assert "hook['install_host_network'](policy, config['broker_required'])" in before
    assert "os._exit(78)" in before
    assert "site.main()" in before
    assert "runpy.run_module(config['module']" in after
    assert gaps == set()
    assert len(subprocess.list2cmdline(actual)) < 30000


def test_captured_startup_and_isolation_flags_survive():
    h, p = sources()
    code = "import sys,runpy;sys.path[:0]=['admitted'];sys.argv=['astrid.core.execution.generic_host_worker',*sys.argv[1:]];" + \
           "runpy.run_module('astrid.core.execution.generic_host_worker',run_name='__main__')"
    argv = (sys.executable, "-I", "-c", code, "--external-source-fd", "91", "request.json")
    result = instrument_argv(argv, h, p, TARGETS, set())
    assert isinstance(result, tuple)
    assert result[:3] == argv[:3] and result[4:] == argv[4:]
    assert result[3].startswith(code[:code.rfind("runpy.run_module")])
    assert result[3].count(MARKER) >= 1
    assert instrument_argv(result, h, p, TARGETS, set()) == result


def test_unknown_worker_startup_is_not_replaced_and_is_unknown():
    h, p = sources()
    argv = [sys.executable, "-c", "print('astrid.core.execution.generic_host_worker')"]
    gaps = set()
    assert instrument_argv(argv, h, p, TARGETS, gaps) == argv
    assert gaps == {"unrecognized admitted worker startup"}


def test_failed_admission_is_still_first_and_produces_no_complete_report():
    h, p = sources()
    code = "import os,runpy;os._exit(78);" + \
           "runpy.run_module('astrid.core.execution.generic_host_worker',run_name='__main__')"
    original = [sys.executable, "-I", "-S", "-c", code]
    argv = instrument_argv(original, h, p, TARGETS, set())
    child = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert child.returncode == 78
    assert PREFIX not in child.stderr


def test_deduplicated_tail_reports_and_conflicts_are_explicit():
    row = {"schema": "astrid.local-speed-process.v1", "pid": 10, "start_ns": 20, "counts": {}}
    line = PREFIX + json.dumps(row)
    records, gaps = read_reports([line, line])
    assert records == [row] and gaps == []
    _, gaps = read_reports([line, PREFIX + json.dumps({**row, "counts": {"x": 1}}), PREFIX + "{broken"])
    assert set(gaps) == {"conflicting process report", "malformed or truncated process report"}


def test_event_overflow_is_not_zero_coverage(monkeypatch):
    import tests._local_speed_process as module
    monkeypatch.setattr(module, "MAX_EVENTS", 1)
    meter = observer()
    meter.start()
    try:
        meter.event("one")
        meter.event("two")
        report = meter.snapshot()
    finally:
        meter.stop()
    assert "Python observation event limit exceeded" in report["coverage_gaps"]


def test_profiler_and_hash_constructor_are_restored():
    original = hashlib.sha256
    prior = sys.getprofile()
    meter = observer()
    meter.start()
    meter.stop()
    assert hashlib.sha256 is original
    assert sys.getprofile() is prior


def test_direct_pack_module_is_observed_without_changing_its_entrypoint_semantics(tmp_path):
    from tests._local_speed_process import PACK_MODULE
    root = tmp_path
    for part in PACK_MODULE.split(".")[:-1]:
        root = root / part
        root.mkdir()
        (root / "__init__.py").write_text("")
    (root / "run.py").write_text(
        "import hashlib,sys\nhashlib.sha256(" + repr(DATA) + ").hexdigest()\n"
        "print(sys.argv[1:])\n")
    meter = observer()
    meter.start()
    try:
        child = subprocess.run([sys.executable, "-X", "utf8", "-m", PACK_MODULE, "--job", "owned"],
                               cwd=tmp_path, env={**os.environ, "PYTHONPATH": ""},
                               capture_output=True, text=True, timeout=30)
        parent = meter.snapshot()
    finally:
        meter.stop()
    reports, gaps = read_reports([child.stderr])
    assert child.returncode == 0 and child.stdout.strip() == "['--job', 'owned']"
    assert gaps == [] and len(reports) == 1
    assert reports[0]["role"] == "pack_worker" and reports[0]["output_hash_streams"] == 1
    assert parent["process_starts"][0]["child_role"] == "pack_worker"
    assert child.args[1:3] == ["-X", "utf8"]


def test_unrelated_code_does_not_enter_the_boundary_cache_or_emit_events():
    namespace = {}
    exec(compile("def unrelated(): return 1", "/fixture/irrelevant.py", "exec"), namespace)
    meter = observer()
    meter.start()
    try:
        for _ in range(1000):
            assert namespace["unrelated"]() == 1
        assert id(namespace["unrelated"].__code__) not in meter.code_kinds
        assert "bundle_emission" not in meter.snapshot()["counts"]
    finally:
        meter.stop()


def test_unrelated_call_does_not_inspect_its_filename():
    from types import SimpleNamespace
    class Code:
        co_name = "deepcopy"
        @property
        def co_filename(self):
            pytest.fail("unrelated calls must not format filenames")
    meter = observer()
    meter.profile(SimpleNamespace(f_code=Code()), "call", None)
    assert meter.events == [] and meter.code_kinds == {}


def test_fast_rejection_still_chains_a_preexisting_profiler():
    from types import SimpleNamespace
    import threading
    calls = []
    meter = observer()
    meter.previous_profile = lambda frame, event, arg: calls.append(event)
    meter.start_thread = threading.get_ident()
    meter.chain_profiles = True
    frame = SimpleNamespace(f_code=SimpleNamespace(co_name="deepcopy"))
    for event, arg in (("call", None), ("return", None), ("c_return", {}.get)):
        meter.profile(frame, event, arg)
    assert calls == ["call", "return", "c_return"]


def test_failed_sync_is_not_lost_by_c_function_identity_filter(tmp_path):
    # A negative fd is rejected by Python before os.fsync emits a C exception;
    # a closed, previously valid fd exercises the actual profiled C failure.
    meter = observer()
    meter.start()
    try:
        with (tmp_path / "closed-fsync").open("wb") as handle:
            closed_fd = handle.fileno()
        begin = time.perf_counter_ns()
        with pytest.raises(OSError):
            os.fsync(closed_fd)
        report = meter.snapshot(begin)
    finally:
        meter.stop()
    assert report["counts"]["fsync_attempt"] == 1
    assert report["counts"]["fsync_error"] == 1
    assert report["counts"].get("fsync_success", 0) == 0
