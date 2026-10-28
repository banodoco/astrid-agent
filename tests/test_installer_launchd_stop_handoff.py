"""Real Installer/Backend/C1 handoff with only launchctl observations modeled."""

from __future__ import annotations

import plistlib
import subprocess
import sys
import importlib
from pathlib import Path

import pytest

from astrid.installer import core
from astrid.installer.backend import Backend
from astrid.installer.service import LABEL, command, install_selection, render
from runtime_protocol import platform

from tests.test_installer_lifecycle import _installer_runtime, _publish_owner


class _LaunchdControllerExit(BaseException):
    """Model controller death after C1 exit and before launchd bootout."""


def _version(backend, name: str, release: int) -> core.Version:
    folder = backend.root / "versions" / name
    folder.mkdir(parents=True)
    version = core.Version(folder, Path(sys.executable), folder / "profile.json", {}, release)
    core.write_json(folder / "version.json", version.document())
    return version


def _launchd_harness(backend, data, old, boundary, events):
    """Model launchd's loaded view and print/bootout boundary, not C1 or callers."""
    service = backend.service
    service.system = "darwin"
    service.path = service.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    service.selection = install_selection(service.home, "darwin")
    service.disable = type(service).disable.__get__(service, type(service))
    service.stop = type(service).stop.__get__(service, type(service))
    service.recorded = type(service).recorded.__get__(service, type(service))
    target = f"gui/{platform.current_user()}/{LABEL}"
    loaded = {"version": old, "running": True}
    print_observations = []
    bootouts = []
    crash_after_exit = {"enabled": False}

    def print_output():
        selected = loaded["version"]
        argv = loaded.get("action") or command(selected, data, log=service.log, env_file=service.env_file)
        state = "running" if loaded["running"] and boundary.live else "not running"
        lines = [
            f"{target} = {{",
            f"\tpath = {service.path}",
            f"\tstate = {state}",
            f"\tprogram = {argv[0]}",
        ]
        if loaded["running"] and boundary.live:
            lines.append("\tpid = 777")
        lines.extend(("\targuments = {", "\n".join(argv), "\t}"))
        lines.append("}")
        return "\n".join(lines)

    def run(argv):
        argv = [str(value) for value in argv]
        if argv[:2] == ["launchctl", "print"]:
            assert argv[2] == target
            if loaded["version"] is None:
                raise subprocess.CalledProcessError(113, argv, output="Could not find service")
            value = print_output()
            print_observations.append(value)
            events.append("launchctl-print-running" if boundary.live else "launchctl-print-stopped")
            if crash_after_exit["enabled"] and not boundary.live:
                crash_after_exit["enabled"] = False
                raise _LaunchdControllerExit("controller exited before bootout")
            return value
        if argv[:2] == ["launchctl", "bootout"]:
            assert argv[2] == target
            assert not boundary.live, "bootout must follow Runtime C1 process exit"
            assert loaded["version"] is not None
            bootouts.append(tuple(argv))
            events.append("launchctl-bootout")
            loaded["version"] = None
            return ""
        if argv[:2] == ["launchctl", "bootstrap"]:
            assert argv[2] == f"gui/{platform.current_user()}"
            plist_path = Path(argv[3])
            assert plist_path == service.path
            # The registered plist is the canonical action for the newly
            # selected version. Production render/command remain in use.
            action = plistlib.loads(plist_path.read_bytes())["ProgramArguments"]
            versions = [core.Version.load(core.read_json(path / "version.json"))
                        for path in (backend.root / "versions").glob("*")
                        if (path / "version.json").is_file()]
            loaded["version"] = next(
                version for version in versions
                if command(version, data, log=service.log, env_file=service.env_file) == action
            )
            loaded["running"] = False
            events.append("launchctl-bootstrap")
            return ""
        if argv[:2] == ["launchctl", "kickstart"]:
            assert argv[2] == target
            events.append("launchctl-kickstart")
            return ""
        raise AssertionError(f"Unexpected platform command: {argv!r}")

    service.run = run
    service.path.parent.mkdir(parents=True, exist_ok=True)
    service.path.write_text(
        render(
            "darwin", old, data, service.log, env_file=service.env_file,
        ),
        encoding="utf-8",
    )
    # The production service owner record is read from the selected install
    # identity. _installer_runtime supplies the strict record API used by stop.
    core.write_json(service.selection, {
        "owner": LABEL, "program_root": str(backend.root), "data_root": str(data),
    })
    return loaded, print_observations, bootouts, target, crash_after_exit


def _real_c1_stop(data, boundary, events):
    from astrid_runtime.paths import RuntimePaths

    stop_owner = boundary.stop_owner

    def exit_under_c1(**kwargs):
        events.append("c1-stop-owner")
        boundary.live = False
        # Runtime's C1 handoff must observe the process exit and its removed
        # publications before it invokes the service-manager callback.
        runtime_paths = RuntimePaths.current(data_root=data)
        runtime_paths.discovery_path.unlink(missing_ok=True)
        runtime_paths.instance_lock_path.unlink(missing_ok=True)
        return stop_owner(**kwargs)

    boundary.stop_owner = exit_under_c1


def _assert_exact_loaded_identity(output, service, version, data, target, *, running):
    argv = command(version, data, log=service.log, env_file=service.env_file)
    assert f"{target} = {{" in output
    assert f"path = {service.path}" in output
    assert f"program = {argv[0]}" in output
    assert "\tstate = running" in output if running else "\tstate = not running" in output
    assert "arguments = {\n" + "\n".join(argv) + "\n\t}" in output
    assert ("pid = 777" in output) is running


def test_shared_update_bootouts_exact_loaded_job_after_real_c1_and_completes(tmp_path, monkeypatch):
    backend, data, paths, _realm, boundary, _manager_calls = _installer_runtime(tmp_path, monkeypatch)
    old, candidate = _version(backend, "old", 17), _version(backend, "candidate", 18)
    core.switch(backend.root, old)
    _publish_owner(paths)
    backend.has_workspace = lambda *_: True
    backend.workspace = lambda *_: {"realm_id": "realm-1", "realm_root": str(paths.realms_dir / "realm-1")}
    backend.stage = lambda *_: candidate
    backend.validate_candidate = lambda *_: None
    backend.backup = lambda *_: None
    backend.upgrade = lambda *_: None
    backend.verify = lambda *_args, **_kwargs: None
    events = []
    _real_c1_stop(data, boundary, events)
    loaded, observations, bootouts, target, _crash = _launchd_harness(
        backend, data, old, boundary, events,
    )

    core.Installer(backend.root, data, backend, say=lambda _line: None).run("update", {})

    assert events.index("c1-stop-owner") < events.index("launchctl-bootout")
    assert bootouts == [("launchctl", "bootout", target)]
    assert observations and "pid = 777" in observations[0]
    _assert_exact_loaded_identity(observations[0], backend.service, old, data, target, running=True)
    _assert_exact_loaded_identity(
        next(output for output in observations[1:] if "state = not running" in output),
        backend.service, old, data, target, running=False,
    )
    assert any("state = not running" in output and "pid =" not in output for output in observations[1:])
    assert core.current(backend.root) == candidate
    assert core.read_json(backend.root / "update-transaction.json")["step"] == "complete"
    assert loaded["version"] == candidate


def test_fresh_recovery_bootouts_stopped_job_once_and_is_repeatable(tmp_path, monkeypatch):
    backend, data, paths, _realm, boundary, _manager_calls = _installer_runtime(tmp_path, monkeypatch)
    old, candidate = _version(backend, "old", 17), _version(backend, "candidate", 18)
    core.switch(backend.root, old)
    _publish_owner(paths)
    backend.has_workspace = lambda *_: True
    backend.workspace = lambda *_: {"realm_id": "realm-1", "realm_root": str(paths.realms_dir / "realm-1")}
    backend.stage = lambda *_: candidate
    backend.validate_candidate = lambda *_: None
    events = []
    _real_c1_stop(data, boundary, events)
    loaded, observations, bootouts, target, crash = _launchd_harness(
        backend, data, old, boundary, events,
    )
    installer = core.Installer(backend.root, data, backend, say=lambda _line: None)
    crash["enabled"] = True
    with pytest.raises(_LaunchdControllerExit, match="before bootout"):
        installer.run("update", {})
    assert core.read_json(backend.root / "update-transaction.json")["step"] == "stopping"
    assert not bootouts

    # Recovery uses a new Backend/Service after C1 exited and the controller
    # died before bootout. The process exit and removed publications came from
    # the first real C1 stop above.
    fresh = Backend(
        backend.root, tmp_path / "install.log", tmp_path / "uv",
    )
    fresh.service.system = "darwin"
    fresh.service.path = fresh.service.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    fresh.service.selection = install_selection(fresh.service.home, "darwin")
    fresh.service.run = backend.service.run
    fresh_boundary = boundary
    fresh._stop_boundary = fresh_boundary
    fresh.verify = lambda *_args, **_kwargs: None

    bootstrap = importlib.import_module("astrid_runtime.bootstrap")
    real_down = bootstrap.down

    def tracked_down(*args, **kwargs):
        events.append("fresh-c1-down-enter")
        result = real_down(*args, **kwargs)
        events.append("fresh-c1-down-return")
        return result

    monkeypatch.setattr(bootstrap, "down", tracked_down)

    start = len(events)
    core.Installer(backend.root, data, fresh, say=lambda _line: None)._recover_update()
    terminal = core.read_json(backend.root / "update-transaction.json")
    assert terminal["step"] == "rolled_back"
    assert core.current(backend.root) == old
    assert bootouts == [("launchctl", "bootout", target)]
    recovery_events = events[start:]
    assert recovery_events[0] == "launchctl-print-stopped"
    assert recovery_events.index("launchctl-print-stopped") < recovery_events.index("fresh-c1-down-enter")
    assert events.index("fresh-c1-down-enter") < events.index("launchctl-bootout")
    assert events.index("launchctl-bootout") < events.index("fresh-c1-down-return")
    assert observations and "state = running" in observations[0] and "pid = 777" in observations[0]
    assert all("state = not running" in output and "pid =" not in output
               for output in observations[1:])
    _assert_exact_loaded_identity(observations[-1], backend.service, old, data, target, running=False)

    before = list(bootouts)
    core.Installer(backend.root, data, fresh, say=lambda _line: None)._recover_update()
    assert core.read_json(backend.root / "update-transaction.json")["step"] == "rolled_back"

    assert bootouts == before

    # Recreate the old Runtime process boundary after recovery, then retry the
    # update through the fresh controller. The retry gets its own loaded-job
    # validation and one bootout for that loaded identity.
    boundary.live = True
    _publish_owner(paths)
    loaded["version"], loaded["running"] = old, True
    fresh.has_workspace = backend.has_workspace
    fresh.workspace = backend.workspace
    fresh.stage = backend.stage
    fresh.validate_candidate = backend.validate_candidate
    fresh.backup = lambda *_: None
    fresh.upgrade = lambda *_: None
    fresh.verify = lambda *_args, **_kwargs: None
    core.Installer(backend.root, data, fresh, say=lambda _line: None).run("update", {})
    assert core.current(backend.root) == candidate
    assert core.read_json(backend.root / "update-transaction.json")["step"] == "complete"
    assert bootouts == [
        ("launchctl", "bootout", target), ("launchctl", "bootout", target),
    ]


@pytest.mark.parametrize("failure_point", ["backup", "candidate_verify"])
def test_downstream_update_failure_runs_real_c1_rollback_and_keeps_old_selected(
        tmp_path, monkeypatch, failure_point):
    backend, data, paths, _realm, boundary, _manager_calls = _installer_runtime(tmp_path, monkeypatch)
    old, candidate = _version(backend, "old", 17), _version(backend, "candidate", 18)
    core.switch(backend.root, old)
    _publish_owner(paths)
    backend.has_workspace = lambda *_: True
    backend.workspace = lambda *_: {"realm_id": "realm-1", "realm_root": str(paths.realms_dir / "realm-1")}
    backend.stage = lambda *_: candidate
    backend.validate_candidate = lambda *_: None
    def backup(*_args):
        if failure_point == "backup":
            raise RuntimeError("update failed downstream")

    def verify(version, *_args, **_kwargs):
        if failure_point == "candidate_verify" and version == candidate:
            raise core.VerificationError("update failed downstream")

    backend.backup = backup
    backend.upgrade = lambda *_: None
    backend.verify = verify
    events = []
    _real_c1_stop(data, boundary, events)
    _loaded, observations, bootouts, target, _crash = _launchd_harness(
        backend, data, old, boundary, events,
    )

    with pytest.raises((RuntimeError, core.VerificationError), match="update failed downstream"):
        core.Installer(backend.root, data, backend, say=lambda _line: None).run("update", {})

    assert events.index("c1-stop-owner") < events.index("launchctl-bootout")
    assert bootouts == [("launchctl", "bootout", target)] * (2 if failure_point == "candidate_verify" else 1)
    assert observations and "pid = 777" in observations[0]
    _assert_exact_loaded_identity(observations[0], backend.service, old, data, target, running=True)
    assert any("state = not running" in output and "pid =" not in output for output in observations[1:])
    assert core.current(backend.root) == old
    assert _loaded["version"] == old
    assert core.read_json(backend.root / "update-transaction.json")["step"] == "rolled_back"

@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("indent_arguments", [False, True])
def test_cached_retained_action_survives_canonical_plist_publication(
        tmp_path, monkeypatch, bound, indent_arguments):
    from tests.test_installer_service_env import _legacy_posix_action

    backend, data, paths, _realm, boundary, _manager_calls = _installer_runtime(tmp_path, monkeypatch)
    old = _version(backend, "old", 17)
    _publish_owner(paths)
    events = []
    _real_c1_stop(data, boundary, events)
    loaded, observations, bootouts, target, _crash = _launchd_harness(
        backend, data, old, boundary, events)
    operation = "a" * 32 if bound else None
    if operation:
        core.write_json(backend.root / "update-transaction.json", {"operation_id": operation})
    loaded["action"] = _legacy_posix_action(old, data, backend.service.env_file, operation=operation)
    if indent_arguments:
        real_run = backend.service.run

        def indented_print(argv):
            value = real_run(argv)
            if argv[:2] == ["launchctl", "print"]:
                prefix, arguments = value.split("\targuments = {\n", 1)
                body, suffix = arguments.rsplit("\n\t}", 1)
                return prefix + "\targuments = {\n" + "\n".join(
                    "\t\t" + line for line in body.split("\n")) + "\n\t}" + suffix
            return value

        backend.service.run = indented_print

    backend.publish_launch_admission(old, data)
    backend.stop(old, data)

    assert observations and loaded["action"][-1] in observations[0]
    assert boundary.stop_calls == 1
    assert bootouts == [("launchctl", "bootout", target)]
    assert events.index("c1-stop-owner") < events.index("launchctl-bootout")

