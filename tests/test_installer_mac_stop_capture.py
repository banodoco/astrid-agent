import json

import pytest

from astrid.installer.core import RuntimeOperationError
from astrid.installer.mac_stop_capture import MacStopCapture, ObservedBoundary
from runtime_protocol import platform
from tests.test_installer_lifecycle import _installer_runtime, _publish_owner
from tests.test_installer_launchd_stop_handoff import _launchd_harness, _real_c1_stop, _version


def test_capture_bounds_redacts_and_never_follows_discovery_symlink(tmp_path, monkeypatch):
    discovery = tmp_path / "discovery"
    log = tmp_path / "log"
    owner = {"pid": 42, "process_birth_id": "private-birth", "runtime_instance_id": "private-instance",
             "endpoint": "secret-endpoint", "token": "secret-token"}
    discovery.write_text(json.dumps(owner))
    monkeypatch.setattr(platform, "process_identity", lambda _pid: {
        "pid": 42, "uid": platform.current_user(), "birth_id": "private-birth"})
    capture = MacStopCapture(log, discovery, owner)
    capture.event("pre_admission")
    discovery.write_text("x" * 65537)
    capture.event("down_enter")
    discovery.unlink()
    discovery.symlink_to(tmp_path / "missing")
    capture.event("stop_error")
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert [row["discovery"] for row in rows] == ["same", "oversize", "unreadable"]
    assert [row["process"] for row in rows] == ["same"] * 3
    assert [row["seq"] for row in rows] == [1, 2, 3]
    assert all(row["sample_end_monotonic_ns"] >= row["monotonic_ns"] for row in rows)
    for secret in ("private-birth", "private-instance", "secret-endpoint", "secret-token"):
        assert secret not in log.read_text()


def test_capture_observes_bootout_and_runtime_stop_order(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_INSTALLER_MAC_STOP_CAPTURE", "1")
    backend, data, paths, _realm, boundary, _calls = _installer_runtime(tmp_path, monkeypatch)
    old = _version(backend, "old", 17)
    _publish_owner(paths)
    actions = []
    _real_c1_stop(data, boundary, actions)
    _launchd_harness(backend, data, old, boundary, actions, monkeypatch)
    backend.stop(old, data)
    rows = [json.loads(line) for line in backend.log.read_text().splitlines()]
    assert [row["stage"] for row in rows] == [
        "pre_admission", "preflight_return", "bootout_enter", "bootout_return",
        "down_enter", "runtime_stop_enter", "runtime_stop_return", "down_return"]
    assert "discovery" not in rows[2]
    assert rows[6]["discovery"] == "absent"
    assert rows[5]["runtime_owner"] == rows[6]["runtime_owner"] == "same"
    assert len({row["capture"] for row in rows}) == 1
    assert actions.index("launchctl-bootout") < actions.index("c1-stop-owner")


def test_capture_preserves_refusal_without_bootout(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_INSTALLER_MAC_STOP_CAPTURE", "1")
    backend, data, paths, _realm, boundary, _calls = _installer_runtime(tmp_path, monkeypatch)
    old = _version(backend, "old", 17)
    _publish_owner(paths)
    actions = []
    _launchd_harness(backend, data, old, boundary, actions, monkeypatch)
    monkeypatch.setattr(platform, "process_identity", lambda pid: {
        "pid": pid, "uid": platform.current_user(), "birth_id": "foreign"})
    with pytest.raises(RuntimeOperationError):
        backend.stop(old, data)
    rows = [json.loads(line) for line in backend.log.read_text().splitlines()]
    assert [row["stage"] for row in rows] == ["pre_admission", "preflight_return", "stop_error"]
    assert "launchctl-bootout" not in actions
    assert boundary.stop_calls == 0


def test_capture_brackets_discovery_loss_during_bootout(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_INSTALLER_MAC_STOP_CAPTURE", "1")
    backend, data, paths, _realm, boundary, _calls = _installer_runtime(tmp_path, monkeypatch)
    old = _version(backend, "old", 17)
    _publish_owner(paths)
    actions = []
    _launchd_harness(backend, data, old, boundary, actions, monkeypatch)
    run = backend.service.run

    def bootout_removes_discovery(argv):
        result = run(argv)
        if argv[:2] == ["launchctl", "bootout"]:
            paths.discovery_path.unlink()
        return result

    backend.service.run = bootout_removes_discovery
    with pytest.raises(RuntimeOperationError):
        backend.stop(old, data)
    rows = {row["stage"]: row for row in map(json.loads, backend.log.read_text().splitlines())}
    assert rows["preflight_return"]["discovery"] == "same"
    assert rows["bootout_return"]["discovery"] == "absent"
    assert rows["bootout_return"]["process"] == "same"
    assert "runtime_stop_enter" not in rows
    assert rows["stop_error"]["discovery"] == "absent"
    assert boundary.stop_calls == 0


def test_capture_failure_does_not_replace_runtime_exception(tmp_path):
    capture = MacStopCapture(tmp_path / "missing" / "log", tmp_path / "discovery", None)

    class Boundary:
        def stop_owner(self, **_kwargs):
            raise ValueError("original")

    with pytest.raises(ValueError, match="original"):
        ObservedBoundary(Boundary(), capture).stop_owner()
