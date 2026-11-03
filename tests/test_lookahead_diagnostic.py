"""Temporary probe checks; this branch is diagnostic only and must not land."""

from pathlib import Path
from unittest.mock import Mock

from astrid.installer.backend import Backend
from astrid.installer.core import Version


def test_managed_install_skips_live_owner_identity(tmp_path, monkeypatch):
    backend = Backend(tmp_path / "program", tmp_path / "log", tmp_path / "uv")
    version = Version(tmp_path / "program/versions/v1", tmp_path / "python",
                      tmp_path / "profile", {})
    monkeypatch.setattr(backend, "_discovery_owner_state", Mock(side_effect=AssertionError("identity read")))
    data = tmp_path / "data"
    discovery = data / "runtime" / "discovery.json"
    discovery.parent.mkdir(parents=True)
    discovery.write_text("invalid discovery")
    assert backend.source_checkout_ownership(data, version) is False
    backend._discovery_owner_state.assert_not_called()


def test_launch_admission_does_not_rewrite_old_definition(tmp_path):
    backend = Backend(tmp_path / "program", tmp_path / "log", tmp_path / "uv")
    backend.service.publish_launch_admission = Mock(side_effect=AssertionError("rewrote R0"))
    version = Version(tmp_path / "program/versions/v1", tmp_path / "python",
                      tmp_path / "profile", {})
    backend.publish_launch_admission(version, tmp_path / "data")
    backend.service.publish_launch_admission.assert_not_called()


def test_launchd_keepalive_restart_boots_out_before_stopped_check(tmp_path, monkeypatch):
    from tests.test_installer_lifecycle import _installer_runtime, _publish_owner
    from tests.test_installer_launchd_stop_handoff import _launchd_harness, _real_c1_stop, _version

    backend, data, paths, _realm, boundary, _calls = _installer_runtime(tmp_path, monkeypatch)
    version = _version(backend, "old", 17)
    _publish_owner(paths)
    events = []
    _real_c1_stop(data, boundary, events)
    loaded, _observations, bootouts, target, _crash = _launchd_harness(
        backend, data, version, boundary, events)
    run = backend.service.run

    def keepalive_restart(argv):
        output = run(argv)
        if argv[:2] == ["launchctl", "print"] and not boundary.live and loaded["version"] is not None:
            events.append("keepalive-running")
            output = output.replace("\tstate = not running\n", "\tstate = running\n\tpid = 888\n")
        return output

    backend.service.run = keepalive_restart
    backend.stop(version, data)
    assert bootouts == [("launchctl", "bootout", target)]
    assert events.index("c1-stop-owner") < events.index("keepalive-running") < events.index("launchctl-bootout")
    assert loaded["version"] is None
