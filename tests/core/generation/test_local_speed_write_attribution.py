"""Native attribution never equates a COMMIT or Python call with an OS sync."""

from tests._local_speed_native import attribute_writes, count_interval, trace_strace
from tests.core.generation.test_local_speed_process_observer import observer


def test_precise_windows_epoch_keeps_filetime_fraction_without_coarse_clock_fallback(monkeypatch):
    import ctypes

    from tests import _local_speed_process as process

    expected = 1794961460735565500
    ticks = expected // 100 + 116444736000000000

    def precise(pointer):
        value = ctypes.cast(pointer, ctypes.POINTER(process._FileTime)).contents
        value.low, value.high = ticks & 0xffffffff, ticks >> 32

    monkeypatch.setattr(process, "_precise_file_time", precise)
    monkeypatch.setattr(process.time, "time_ns", lambda: 1794961460734140200)
    assert ctypes.sizeof(process._FileTime) == 8
    assert process._windows_epoch_time_ns() == expected


def test_calibration_keeps_original_delete_interval_separate_from_wal(tmp_path, monkeypatch, capsys):
    import json
    import os
    import sys

    from tests import local_speed_native_calibration as calibration

    monkeypatch.setattr(sys, "argv", ["calibration", "--output", str(tmp_path)])
    monkeypatch.setattr(os, "fsync", os.fsync)  # Restore even if the calibration fails.
    calibration.main()
    original = json.loads((tmp_path / "calibration-interval.json").read_text())
    wal = json.loads((tmp_path / "wal-calibration.json").read_text())
    proof = json.loads(capsys.readouterr().out)
    assert original["epoch_start_ns"] < original["epoch_reply_ns"] < wal["interval"]["epoch_start_ns"]
    assert wal["interval"]["epoch_start_ns"] < wal["checkpoint_start_ns"] < wal["interval"]["epoch_reply_ns"]
    assert proof["explicit_python_fsync_calls"] == 1
    assert proof["sqlite_commits_exercised"] == 2
    assert proof["stored_result_verified"] is True
    assert wal["sqlite"] == {"journal_mode": "wal", "synchronous": 2, "wal_autocheckpoint": 1000}
    assert wal["checkpoint"][0] == 0


def test_shared_runtime_fsync_shim_retains_the_agent_producers_identity(tmp_path):
    import os

    shim = {"os": os}
    exec(compile("def fsync(fd):\n    return os.fsync(fd)\n", "/pinned/runtime_protocol/dirfd.py", "exec"), shim)
    producers = []
    for path in ("/candidate/astrid/core/execution/generic_host.py", "/candidate/astrid/sdk/invocation.py",
                 "/candidate/astrid/core/generation/engine_custody.py"):
        namespace = {"shim": shim["fsync"]}
        exec(compile("def write(fd):\n    return shim(fd)\n", path, "exec"), namespace)
        producers.append(namespace["write"])
    meter = observer()
    with (tmp_path / "evidence").open("wb") as handle:
        handle.write(b"actual synced bytes")
        handle.flush()
        meter.start()
        try:
            for write in producers:
                write(handle.fileno())
            row = meter.snapshot()
        finally:
            meter.stop()
    assert row["counts"]["fsync_success"] == 3
    assert [span["origin"] for span in row["write_spans"]] == ["agent_host_evidence", "agent_sdk_evidence", "agent_other"]
    assert [span["write_context"] for span in row["write_spans"]] == ["agent_host_evidence", "agent_sdk_evidence", "agent_other"]


def test_resumed_native_write_retains_its_fd_path(tmp_path):
    (tmp_path / "trace.1").write_text(
        "1.100000 fdatasync(4</tmp/realm.sqlite-wal> <unfinished ...>\n"
        "1.200000 <... fdatasync resumed>) = 0 <0.100000>\n"
        "2.000000 +++ exited with 0 +++\n")
    event = trace_strace(tmp_path)["events"][0]
    assert event["fd"] == 4
    assert event["path"] == "/tmp/realm.sqlite-wal"
    assert event["success"] is True


def test_wal_commit_checkpoint_and_publication_are_separate_measured_facts(tmp_path):
    (tmp_path / "trace.1").write_text(
        "1.100000 fdatasync(4</tmp/realm.sqlite-wal>) = 0 <0.001000>\n"
        "1.200000 fdatasync(4</tmp/realm.sqlite-wal>) = 0 <0.001000>\n"
        "1.300000 fdatasync(3</tmp/realm.sqlite>) = 0 <0.001000>\n"
        "1.400000 fsync(5</tmp/output>) = 0 <0.001000>\n"
        "2.000000 +++ exited with 0 +++\n")
    native = count_interval(trace_strace(tmp_path), {"pid": 1, "epoch_start_ns": 1_000_000_000, "epoch_reply_ns": 1_900_000_000})
    observation = {"sqlite": {"database": "/tmp/realm.sqlite"}, "write_spans": [
        {"pid": 1, "tid": 1, "kind": "runtime_outer_commit_success", "epoch_start_ns": 1_090_000_000, "epoch_end_ns": 1_110_000_000,
         "write_context": "http_request", "route": "POST /v1/tasks"},
        {"pid": 1, "tid": 1, "kind": "runtime_background_span", "epoch_start_ns": 1_190_000_000, "epoch_end_ns": 1_310_000_000,
         "write_context": "background_lifetime_reaper"},
        {"pid": 1, "tid": 1, "kind": "application_sync", "epoch_start_ns": 1_390_000_000, "epoch_end_ns": 1_410_000_000,
         "success": True, "origin": "runtime_publication", "write_context": "http_request",
         "route": "POST /v1/attempts/<attempt>/settle"},
    ]}
    result = attribute_writes(native, observation)
    assert result["status"] == "PASS"
    assert result["counts"]["sqlite_wal_during_commit"] == 1
    assert result["counts"]["sqlite_wal_outside_commit"] == 1
    assert result["counts"]["sqlite_checkpoint_database"] == 1
    assert result["counts"]["runtime_publication"] == 1
    assert sum(result["counts"].values()) == native["native_flushes"] == 4
    assert result["contexts"]["background_lifetime_reaper"] == 2
    assert result["contexts"]["http_request"] == 2
    assert sum(result["contexts"].values()) == 4


def test_unmatched_native_write_is_not_labelled_background(tmp_path):
    (tmp_path / "trace.1").write_text("1.100000 fsync(4</tmp/unknown>) = 0 <0.001000>\n2.000000 +++ exited with 0 +++\n")
    native = count_interval(trace_strace(tmp_path), {"pid": 1, "epoch_start_ns": 0, "epoch_reply_ns": 2_000_000_000})
    result = attribute_writes(native, {"sqlite": {"database": "/tmp/db"}, "write_spans": []})
    assert result["status"] == "NO-RESULT"
    assert len(result["unattributed"]) == 1
    assert result["counts"]["runtime_background"] == 0


def test_known_wal_path_without_request_or_background_scope_stays_unknown(tmp_path):
    (tmp_path / "trace.1").write_text("1.100000 fdatasync(4</tmp/db-wal>) = 0 <0.001000>\n2.000000 +++ exited with 0 +++\n")
    native = count_interval(trace_strace(tmp_path), {"pid": 1, "epoch_start_ns": 0, "epoch_reply_ns": 2_000_000_000})
    result = attribute_writes(native, {"sqlite": {"database": "/tmp/db"}, "write_spans": []})
    assert result["status"] == "NO-RESULT"
    assert result["counts"]["sqlite_wal_outside_commit"] == 1
    assert result["contexts"]["unattributed_runtime"] == 1
    assert result["contexts"]["background_lifetime_reaper"] == 0
