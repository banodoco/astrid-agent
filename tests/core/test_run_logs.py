import subprocess
import sys
from pathlib import Path

import pytest

from astrid.core._shared import run_logs


def test_declare_round_trip_and_replace(tmp_path):
    source = tmp_path / "one.log"
    run_logs.declare(tmp_path, run_logs.LogDecl("run", "run", str(source)))
    assert run_logs.read_declared(tmp_path) == [(run_logs.LogDecl("run", "run", str(source)), source)]
    run_logs.declare(tmp_path, run_logs.LogDecl("run", "run", "other.log", start=7))
    assert len(run_logs.read_declared(tmp_path)) == 1
    assert run_logs.read_declared(tmp_path)[0][0].path == "other.log"


def test_collect_byte_slice_and_start_after_replacement(tmp_path):
    source = tmp_path / "shared.log"
    source.write_bytes(b"x" * 100)
    run_logs.declare(tmp_path, run_logs.LogDecl("run", "run", str(source), start=100))
    with source.open("ab") as stream:
        stream.write(b"this run\n")
    run_logs.collect(tmp_path)
    assert (tmp_path / "logs/run.log").read_bytes() == b"this run\n"
    run_logs.declare(tmp_path, run_logs.LogDecl("run", "run", str(source), start=10_000))
    source.write_bytes(b"replaced\n")
    run_logs.collect(tmp_path)
    assert (tmp_path / "logs/run.log").read_bytes() == b"replaced\n"


def test_collect_child_relative_path(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    (child / "child.log").write_bytes(b"child")
    run_logs.declare(child, run_logs.LogDecl("child", "run", "child.log"))
    run_logs.declare_child(tmp_path, child / "logs.json")
    rows = run_logs.collect(tmp_path)
    assert rows[0]["source"] == str(child / "child.log")
    assert (tmp_path / "logs/child.log").read_bytes() == b"child"


def test_missing_and_symlink_sources_are_rows(tmp_path):
    run_logs.declare(tmp_path, run_logs.LogDecl("missing", "run", "absent.log"))
    target = tmp_path / "target.log"
    target.write_text("secret")
    link = tmp_path / "link.log"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    run_logs.declare(tmp_path, run_logs.LogDecl("link", "run", "link.log"))
    rows = run_logs.collect(tmp_path)
    assert all(row["missing"] for row in rows)
    assert {row["name"] for row in rows} == {"missing", "link"}


def test_cap_keeps_head_and_tail_with_gap(tmp_path):
    source = tmp_path / "large.log"
    source.write_bytes(bytes(range(256)) * 8)
    run_logs.declare(tmp_path, run_logs.LogDecl("run", "run", str(source)))
    row = run_logs.collect(tmp_path, cap_bytes=1024)[0]
    payload = (tmp_path / "logs/run.log").read_bytes()
    assert row["truncated"] is True
    assert len(payload) <= 1024
    assert payload.startswith(source.read_bytes()[:500])
    assert payload.endswith(source.read_bytes()[-500:])
    assert b"[run-logs gap]" in payload


def test_declare_child_rejects_outside_root(tmp_path):
    with pytest.raises(ValueError):
        run_logs.declare_child(tmp_path / "root", tmp_path / "outside" / "logs.json")


def test_script_mode_matches_function(tmp_path):
    source = Path(run_logs.__file__).read_text(encoding="utf-8")
    root = tmp_path / "script"
    log = root / "attempt.json"
    root.mkdir()
    log.write_text("{}")
    run_logs.declare(root, run_logs.LogDecl("run", "run", "attempt.json"))
    expected = run_logs.collect(root)
    expected_index = (root / "logs/index.json").read_bytes()
    (root / "logs/index.json").unlink()
    proc = subprocess.run([sys.executable, "-c", source, "collect", str(root)], capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()
    assert run_logs.read_index(root) == expected
    assert (root / "logs/index.json").read_bytes() == expected_index


def test_literal_format_guard(tmp_path):
    path = tmp_path / "logs.json"
    path.write_text('{"schema": "run-logs.v1", "logs": [{"name": "run", "role": "run", "path": "attempt.json"}], "children": []}')
    assert run_logs.read_declared(tmp_path)[0][0] == run_logs.LogDecl("run", "run", "attempt.json")


def test_time_window_keeps_interleaved_lines(tmp_path):
    source = tmp_path / "shared.log"
    source.write_text("2025-01-01T00:00:00Z run-b start\n2025-01-01T00:00:01Z run-a start\n2025-01-01T00:00:03Z run-b done\n2025-01-01T00:00:04Z run-a done\n")
    run_logs.declare(tmp_path, run_logs.LogDecl("run", "run", "shared.log", since="2025-01-01T00:00:01Z", until="2025-01-01T00:00:02Z"))
    run_logs.collect(tmp_path)
    result = (tmp_path / "logs/run.log").read_text()
    assert "run-a start" in result
    assert "run-b start" not in result
    assert "run-b done" not in result


def test_time_window_keeps_untimestamped_traceback_continuations(tmp_path):
    source = tmp_path / "shared.log"
    source.write_text("2025-01-01T00:00:01Z run-a error\n traceback line\n2025-01-01T00:00:03Z run-a done\n")
    run_logs.declare(tmp_path, run_logs.LogDecl("run", "run", "shared.log", since="2025-01-01T00:00:01Z", until="2025-01-01T00:00:01Z"))
    run_logs.collect(tmp_path)
    result = (tmp_path / "logs/run.log").read_text()
    assert "run-a error" in result
    assert "traceback line" in result
    assert "run-a done" not in result


def test_bytes_then_time_selection(tmp_path):
    source = tmp_path / "shared.log"
    source.write_text("skip\n2025-01-01T00:00:00Z outside\n2025-01-01T00:00:01Z inside\n")
    run_logs.declare(tmp_path, run_logs.LogDecl("run", "run", "shared.log", start=5, since="2025-01-01T00:00:01Z", until="2025-01-01T00:00:01Z"))
    run_logs.collect(tmp_path)
    result = (tmp_path / "logs/run.log").read_text()
    assert "inside" in result
    assert "outside" not in result
    assert "skip" not in result
