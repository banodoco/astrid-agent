"""Native attribution never equates a COMMIT or Python call with an OS sync."""

from tests._local_speed_native import attribute_writes, count_interval, trace_strace


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
        {"pid": 1, "tid": 1, "kind": "runtime_outer_commit_success", "epoch_start_ns": 1_090_000_000, "epoch_end_ns": 1_110_000_000},
        {"pid": 1, "tid": 1, "kind": "application_sync", "epoch_start_ns": 1_390_000_000, "epoch_end_ns": 1_410_000_000,
         "success": True, "origin": "runtime_publication", "route": "POST /v1/attempts/<attempt>/settle"},
    ]}
    result = attribute_writes(native, observation)
    assert result["status"] == "PASS"
    assert result["counts"]["sqlite_wal_during_commit"] == 1
    assert result["counts"]["sqlite_wal_outside_commit"] == 1
    assert result["counts"]["sqlite_checkpoint_database"] == 1
    assert result["counts"]["runtime_publication"] == 1
    assert sum(result["counts"].values()) == native["native_flushes"] == 4


def test_unmatched_native_write_is_not_labelled_background(tmp_path):
    (tmp_path / "trace.1").write_text("1.100000 fsync(4</tmp/unknown>) = 0 <0.001000>\n2.000000 +++ exited with 0 +++\n")
    native = count_interval(trace_strace(tmp_path), {"pid": 1, "epoch_start_ns": 0, "epoch_reply_ns": 2_000_000_000})
    result = attribute_writes(native, {"sqlite": {"database": "/tmp/db"}, "write_spans": []})
    assert result["status"] == "NO-RESULT"
    assert len(result["unattributed"]) == 1
    assert result["counts"]["runtime_background"] == 0
