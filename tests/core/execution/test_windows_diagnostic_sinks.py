"""Portable diagnostic contracts without POSIX descriptor-flag probes."""
import json
import stat
from types import SimpleNamespace

import pytest

from astrid.core.execution import generic_host
from astrid.sdk import _child_bridge


@pytest.mark.parametrize("endpoint", ["sdk", "host"])
def test_bridge_regular_file_diagnostic_is_redacted_without_fcntl(monkeypatch, endpoint):
    rows = []
    monkeypatch.setattr(_child_bridge, "os", SimpleNamespace(
        fstat=lambda fd: SimpleNamespace(st_mode=stat.S_IFREG),
        write=lambda fd, frame: rows.append((fd, frame)),
    ))
    _child_bridge._emit_bridge_io_failure(
        endpoint=endpoint, phase="send", operation="secret-operation", exception=OSError(5, "secret-message"),
        request_sequence=1, frame_bytes=12, elapsed_seconds=0.1, budget_seconds=1,
    )
    (descriptor, frame), = rows
    row = json.loads(frame)
    assert descriptor == 2 and len(frame) <= 512
    assert row["endpoint"] == endpoint and row["operation"] == "unknown"
    assert row["exception_class"] == "OSError" and row["errno"] == 5
    assert b"secret" not in frame


def test_bridge_unavailable_windows_pipe_flags_cannot_mask_cleanup_error(monkeypatch):
    rows = []
    # Python's Windows pipe APIs vary by version; missing get/set_blocking
    # remains a best-effort diagnostic failure after mandatory cleanup.
    monkeypatch.setattr(_child_bridge, "os", SimpleNamespace(
        fstat=lambda fd: SimpleNamespace(st_mode=stat.S_IFIFO),
        write=lambda fd, frame: rows.append(frame),
    ))
    original = RuntimeError("original cleanup result")
    with pytest.raises(RuntimeError) as caught:
        try:
            raise original
        finally:
            _child_bridge._write_bridge_io_failure(b"diagnostic\n")
    assert caught.value is original and rows == []


@pytest.mark.parametrize("private", [False, True])
def test_windows_startup_diagnostic_requires_private_handle_and_fixed_fields(monkeypatch, private):
    rows = []
    monkeypatch.setattr(generic_host, "_STARTUP_OBSERVATION_ENABLED", True)
    monkeypatch.setattr(generic_host, "os", SimpleNamespace(
        fstat=lambda fd: SimpleNamespace(st_mode=stat.S_IFREG),
        getpid=lambda: 123,
        write=lambda fd, frame: rows.append((fd, frame)),
    ))
    checked = []
    monkeypatch.setattr(generic_host, "owned_by_current_user", lambda fd, info, **kwargs: checked.append((fd, kwargs)) or private)
    generic_host._emit_startup_phase("secret-unadmitted-phase")
    assert rows == [] and checked == []
    generic_host._emit_startup_phase("preflight_begin")
    assert checked == [(2, {"mode": 0o600})]
    if private:
        (descriptor, frame), = rows
        row = json.loads(frame)
        assert descriptor == 2 and len(frame) <= 512
        assert set(row) == {"diagnostic", "phase", "process_id", "elapsed_ms"}
        assert row["diagnostic"] == "astrid.host_startup_phase"
        assert row["phase"] == "preflight_begin" and row["process_id"] == 123
    else:
        assert rows == []


def test_service_startup_timing_reaches_pipe_once_per_phase(monkeypatch, capsys):
    monkeypatch.setattr(generic_host, "_STARTUP_OBSERVATION_ENABLED", True)
    monkeypatch.setattr(generic_host, "_STARTUP_TIMING_SEEN", set())
    monkeypatch.setattr(generic_host, "_STARTUP_TIMING_LAST", 10.0)
    monkeypatch.setattr(generic_host.time, "monotonic", lambda: 10.125)
    monkeypatch.setattr(generic_host, "os", SimpleNamespace(
        fstat=lambda fd: SimpleNamespace(st_mode=stat.S_IFIFO),
    ))
    generic_host._emit_startup_phase("discovery_done")
    generic_host._emit_startup_phase("discovery_done")
    generic_host._emit_startup_phase("/home/private-name/not-a-phase")
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert rows == [{"event": "astrid.host_startup_timing", "phase": "discovery_done",
                     "elapsed_ms": 125}]


def test_foreground_bootstrap_enables_service_timing_when_imported(monkeypatch, capsys):
    from astrid.sdk import host_bootstrap

    monkeypatch.setattr(generic_host, "_STARTUP_OBSERVATION_ENABLED", False)
    monkeypatch.setattr(generic_host, "_STARTUP_TIMING_SEEN", set())
    # A minimal handoff returns before discovery or any runtime connection.
    assert host_bootstrap.ensure_pack_host({}, reconfigure_action="restart", _foreground=True) == {}
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert any(row.get("event") == "astrid.host_startup_timing"
               and row["phase"] == "bootstrap_begin" for row in rows)
