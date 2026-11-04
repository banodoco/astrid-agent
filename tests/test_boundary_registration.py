"""B through real Backend/CLI and persistent simulated systemd, with real locks.

No native service, data adoption, wheel, venv, or download is used by these tests.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from runtime_protocol import platform

from astrid.installer import boundary_registration as registration
from astrid.installer import r0_stop
from astrid.installer.backend import Backend
from astrid.installer.core import Installer, Version, read_json, write_json
from astrid.installer.service import installer_lock_path
from tests.test_installer_r0_entry import invoke, public_case, state
from tests.test_installer_r0_stop import snapshot

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="persistent systemctl simulation")


@pytest.fixture
def case(tmp_path, monkeypatch):
    root, data, realm, service, manager = public_case(tmp_path, monkeypatch)
    shim = tmp_path / "commands/systemctl"
    body = shim.read_text().replace('print("FragmentPath=" + state["unit"])',
        'print("FragmentPath=" + state["unit"])\n'
        '    print("ActiveState=" + ("active" if state["running"] else "inactive"))\n'
        '    print("UnitFileState=" + ("enabled" if state["enabled"] else "disabled"))')
    body = body.replace('print("LoadState=loaded\\nActiveState=" + state["r1"])',
        'print("LoadState=loaded\\nActiveState=" + state["r1"])\n'
        f'    print("FragmentPath=" + state.get("r1_fragment", {str(service.path.with_name(registration.R1_LINUX))!r}))\n'
        '    print("DropInPaths=" + state.get("dropins", ""))\n'
        '    print("UnitFileState=" + ("enabled" if state.get("r1_enabled") else "disabled"))')
    body = body.replace('else:\n    raise SystemExit("unexpected manager mutation: " + repr(args))', f'''
elif args == ["--user", "daemon-reload"]:
    exists = Path({str(service.path.with_name(registration.R1_LINUX))!r}).exists()
    if exists and state["r1"] == "absent":
        state["r1"] = "inactive"
    if not exists:
        state["r1"] = "absent"
    path.write_text(json.dumps(state))
    if state.get("kill_reload"):
        os.kill(os.getppid(), signal.SIGKILL)
elif args == ["--user", "disable", "--now", "astrid-r1.service"]:
    if state.get("fail_r1_stop"):
        raise SystemExit("injected R1 stop failure")
    state.update(r1="inactive", r1_enabled=False)
    path.write_text(json.dumps(state))
else:
    raise SystemExit("unexpected manager mutation: " + repr(args))
''')
    shim.write_text(body)
    backend = Backend(root, service.home / "install.log", Path(sys.executable))
    backend.console_diagnostics = True
    destination = Path(str(data) + "-r1")
    adopted = destination / "runtime/realms/r0"
    adopted.mkdir(parents=True)
    (adopted / "owner.lock").write_bytes(b"R1 lock retained")
    (adopted / "realm.sqlite3").write_bytes(b"R1 accepted data retained")
    # A's pre-existing destination gate is not adoption authority for B. Stop
    # before creating destination in the real C ordering; here move it aside.
    pending = tmp_path / "pending-adopted"
    destination.rename(pending)
    r0_stop.stop_r0_and_wait(backend.service, realm, data_root=data)
    pending.rename(destination)
    folder = root / "versions/release1"
    folder.mkdir()
    version = Version(folder, Path(sys.executable), folder / "profile.json", {
        "TEST_VERSION_ENV": "version one", "ASTRID_RUNTIME_DATA_ROOT": str(data),
        "ASTRID_RUNTIME_DOOR_PORT": "17340",
    }, release=1)
    version.profile.write_text('{}')
    write_json(folder / "version.json", version.document())
    registration.stage_runtime_launcher(version)
    state(manager, calls=[])
    return backend, version, data, realm, destination, manager


def prepare(case):
    backend, version, data, realm, *_ = case
    return backend.prepare_r1_registration(version, data, "r0", realm)


def test_backend_publishes_disabled_distinct_service_then_cli_recovers(case):
    backend, _, data, _, destination, manager = case
    before = snapshot(data), snapshot(destination), backend.service.path.read_bytes()
    journal = prepare(case)
    value = read_json(journal)
    assert value["phase"] == "disabled"
    tx = registration.BoundaryRegistration(backend.service)
    unit = tx.r1_path.read_text()
    assert str(tx.launcher) in unit and str(destination) in unit
    assert str(data) + '"' not in unit
    assert (backend.root / "current").read_text() == "release0\n"
    assert not state(manager)["enabled"] and not state(manager).get("r1_enabled")
    # A registered disabled R1 can still have been started by an interrupted
    # later activation. Recovery must stop it before restarting R0.
    state(manager, r1="active", r1_enabled=True, calls=[])
    result = invoke(backend.root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Recovered the interrupted R1 registration" in result.stdout
    calls = state(manager)["calls"]
    assert calls.index(["--user", "disable", "--now", "astrid-r1.service"]) < calls.index(
        ["--user", "enable", "--now", "astrid.service"])
    assert state(manager)["enabled"] and state(manager)["running"]
    assert state(manager)["r1"] == "absent"
    assert not journal.exists() and not r0_stop._marker_path(backend.service).exists()
    assert not tx.launcher.exists() and not tx.r1_path.exists()
    assert (snapshot(data), snapshot(destination), backend.service.path.read_bytes()) == before


@pytest.mark.parametrize("publication", ["launcher", "definition", "disabled-journal"])
@pytest.mark.parametrize("after", [False, True])
def test_interrupted_publication_is_recovered_by_real_cli(case, monkeypatch, publication, after):
    backend, _, data, _, destination, manager = case
    before = snapshot(data), snapshot(destination)
    tx = registration.BoundaryRegistration(backend.service)
    publish = registration._publish
    class Interrupted(BaseException):
        pass
    def interrupt(path, content, **options):
        selected = ((publication == "launcher" and path == tx.launcher)
                    or (publication == "definition" and path == tx.r1_path)
                    or (publication == "disabled-journal" and path == tx.journal and not options.get("exclusive")))
        if selected and not after:
            raise Interrupted()
        publish(path, content, **options)
        if selected and after:
            raise Interrupted()
    monkeypatch.setattr(registration, "_publish", interrupt)
    with pytest.raises(Interrupted):
        prepare(case)
    assert tx.journal.exists()
    monkeypatch.setattr(registration, "_publish", publish)
    result = invoke(backend.root)
    assert result.returncode == 1 and "Recovered the interrupted R1 registration" in result.stdout
    assert state(manager)["enabled"] and not tx.journal.exists()
    assert (snapshot(data), snapshot(destination)) == before


def kill_after_publication_link(case, publication):
    backend, *_ = case
    tx = registration.BoundaryRegistration(backend.service)
    target = {"journal": tx.journal, "launcher": tx.launcher, "definition": tx.r1_path}[publication]
    child = os.fork()
    if child == 0:
        link = registration.os.link
        def interrupt(source, destination, **options):
            link(source, destination, **options)
            if destination == target:
                os.kill(os.getpid(), signal.SIGKILL)
        registration.os.link = interrupt
        try:
            prepare(case)
        finally:
            os._exit(99)
    _, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    assert target.lstat().st_nlink == 2
    alias = registration._publication_alias(target, read_json(tx.journal))
    assert alias.samefile(target)
    return tx, target, alias


@pytest.mark.parametrize("publication", ["journal", "launcher", "definition"])
@pytest.mark.parametrize("interrupt_cleanup", [False, True])
def test_sigkill_after_link_publication_recovers_on_public_retries(case, publication, interrupt_cleanup):
    backend, _, data, _, destination, manager = case
    before = snapshot(data), snapshot(destination), backend.service.path.read_bytes()
    tx, target, alias = kill_after_publication_link(case, publication)
    assert not state(manager)["enabled"] and not state(manager)["running"]
    if interrupt_cleanup:
        child = os.fork()
        if child == 0:
            unlink = Path.unlink
            def interrupt(path, *args, **options):
                unlink(path, *args, **options)
                if path == alias:
                    os.kill(os.getpid(), signal.SIGKILL)
            Path.unlink = interrupt
            try:
                backend.recover_r1_registration()
            finally:
                os._exit(99)
        _, status = os.waitpid(child, 0)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        assert target.lstat().st_nlink == 1 and not alias.exists()
        assert not state(manager)["enabled"] and tx.journal.exists()
    state(manager, calls=[])
    for attempt in range(2):
        result = invoke(backend.root)
        assert result.returncode == 1, result.stdout + result.stderr
        if attempt == 0:
            assert "Recovered the interrupted R1 registration" in result.stdout
            calls = state(manager)["calls"]
            if publication == "definition":
                assert calls.index(["--user", "disable", "--now", registration.R1_LINUX]) < calls.index(
                    ["--user", "enable", "--now", registration.R0_LINUX])
        assert state(manager)["enabled"] and state(manager)["running"]
        assert state(manager)["r1"] == "absent" and not state(manager).get("r1_enabled")
        assert not any(path.exists() for path in (tx.journal, tx.launcher, tx.r1_path, alias,
                                                 r0_stop._marker_path(backend.service)))
        assert (snapshot(data), snapshot(destination), backend.service.path.read_bytes()) == before


@pytest.mark.parametrize("publication", ["journal", "launcher", "definition"])
@pytest.mark.parametrize("problem", ["foreign-alias", "extra-link", "different-inode", "symlink-alias",
                                    "changed-bytes", "changed-mode", "missing-identity", "invalid-identity"])
def test_interrupted_publication_refuses_unproven_aliases_without_mutation(case, publication, problem):
    backend, _, data, _, destination, manager = case
    tx, target, alias = kill_after_publication_link(case, publication)
    foreign = alias.with_name(".astrid-r1-foreign-link")
    if problem == "foreign-alias":
        alias.rename(foreign)  # Even the old publisher prefix is not authority.
    elif problem == "extra-link":
        os.link(target, foreign)
    elif problem == "different-inode":
        content = alias.read_bytes()
        alias.unlink()
        alias.write_bytes(content)
        os.link(target, foreign)
    elif problem == "symlink-alias":
        alias.rename(foreign)
        alias.symlink_to(target)
    elif problem == "changed-bytes":
        if publication == "journal":
            value = read_json(target)
            value["program_root"] = str(backend.root / "foreign")
            target.write_text(json.dumps(value, sort_keys=True) + "\n")
        else:
            target.write_text("foreign content\n")
    elif problem == "changed-mode":
        target.chmod(0o644)
    else:
        value = read_json(tx.journal)
        if problem == "missing-identity":
            del value["publication_id"]
        else:
            value["publication_id"] = "../foreign"
        tx.journal.write_text(json.dumps(value, sort_keys=True) + "\n")
    # Provision the ordinary CLI lock before recording the installer-state
    # manifest, so its first creation is not mistaken for recovery mutation.
    lock = installer_lock_path(backend.service.home, backend.service.system)
    platform.private_directory(lock.parent)
    lock.touch()
    state(manager, calls=[])
    before = snapshot(data), snapshot(destination), snapshot(backend.root), snapshot(tx.journal.parent), snapshot(tx.r1_path.parent)
    links = {path: path.lstat().st_nlink for path in (target, alias, foreign) if os.path.lexists(path)}
    for _ in range(2):
        result = invoke(backend.root)
        assert result.returncode == 1 and "Recovered the interrupted R1 registration" not in result.stdout
        assert not state(manager)["enabled"] and not state(manager)["running"]
        assert not state(manager)["calls"]
        assert tx.journal.exists() and r0_stop._marker_path(backend.service).exists()
        assert (snapshot(data), snapshot(destination), snapshot(backend.root), snapshot(tx.journal.parent), snapshot(tx.r1_path.parent)) == before
        assert {path: path.lstat().st_nlink for path in links} == links


@pytest.mark.parametrize("publication", ["journal", "launcher", "definition"])
def test_completed_registration_refuses_foreign_hard_links(case, publication):
    backend, _, data, _, destination, manager = case
    prepare(case)
    tx = registration.BoundaryRegistration(backend.service)
    target = {"journal": tx.journal, "launcher": tx.launcher, "definition": tx.r1_path}[publication]
    foreign = backend.root / "foreign-link"
    os.link(target, foreign)
    state(manager, r1="active", r1_enabled=True, calls=[])
    before = snapshot(data), snapshot(destination), snapshot(backend.root), tx.journal.read_bytes()
    for _ in range(2):
        result = invoke(backend.root)
        assert result.returncode == 1 and "Expected a private regular file" in result.stdout
        assert target.samefile(foreign) and target.stat().st_nlink == 2
        assert not state(manager)["enabled"] and state(manager)["r1"] == "active"
        assert not state(manager)["calls"] and r0_stop._marker_path(backend.service).exists()
        assert (snapshot(data), snapshot(destination), snapshot(backend.root), tx.journal.read_bytes()) == before


def test_old_single_link_journal_still_recovers(case):
    backend, *_rest, manager = case
    prepare(case)
    tx = registration.BoundaryRegistration(backend.service)
    value = read_json(tx.journal)
    del value["publication_id"]
    tx.journal.write_text(json.dumps(value, sort_keys=True) + "\n")
    result = invoke(backend.root)
    assert "Recovered the interrupted R1 registration" in result.stdout
    assert state(manager)["enabled"] and not tx.journal.exists()


def test_sigkill_inside_manager_reload_then_cli_replay(case):
    backend, *_rest, manager = case
    prepare(case)
    state(manager, r1="active", r1_enabled=True, kill_reload=True, calls=[])
    killed = invoke(backend.root)
    assert killed.returncode == -signal.SIGKILL
    assert registration.journal_path(backend.service).exists()
    assert not state(manager)["enabled"]
    state(manager, kill_reload=False)
    recovered = invoke(backend.root)
    assert "Recovered the interrupted R1 registration" in recovered.stdout
    assert state(manager)["enabled"]


@pytest.mark.parametrize("problem", ["definition", "launcher", "dropins", "fragment", "unknown", "stop", "lock", "r0-unit", "pointer", "selection"])
def test_uncertain_recovery_retains_intent_and_data_without_restarting_r0(case, monkeypatch, problem):
    backend, _, data, _, destination, manager = case
    prepare(case)
    tx = registration.BoundaryRegistration(backend.service)
    state(manager, r1="active", r1_enabled=True, calls=[])
    fd = None
    if problem == "definition":
        tx.r1_path.write_text("foreign unit")
    elif problem == "launcher":
        tx.launcher.write_text("foreign launcher")
    elif problem == "dropins":
        state(manager, dropins="/foreign/override.conf")
    elif problem == "fragment":
        state(manager, r1_fragment="/foreign/astrid-r1.service")
    elif problem == "unknown":
        state(manager, r1="unknown")
    elif problem == "stop":
        state(manager, fail_r1_stop=True)
    elif problem == "lock":
        fd = os.open(destination / "runtime/realms/r0/owner.lock", os.O_RDONLY)
        platform.lock_file(fd, blocking=False)
        wait = r0_stop._wait_for_owner_lock
        monkeypatch.setattr(r0_stop, "_wait_for_owner_lock", lambda path, *_: wait(path, 0, .2))
    elif problem == "r0-unit":
        backend.service.path.write_text("changed R0 unit")
    elif problem == "pointer":
        (backend.root / "current").write_text("release1\n")
    else:
        selected = read_json(backend.service.selection)
        write_json(backend.service.selection, {**selected, "r1_data_root": str(destination)})
    before = snapshot(data), snapshot(destination)
    try:
        with pytest.raises((ValueError, subprocess.CalledProcessError)):
            backend.recover_r1_registration()
    finally:
        if fd is not None:
            platform.unlock_file(fd)
            os.close(fd)
    assert tx.journal.exists() and not state(manager)["enabled"]
    assert ["--user", "enable", "--now", "astrid.service"] not in state(manager)["calls"]
    if problem == "r0-unit":
        assert ["--user", "disable", "--now", "astrid-r1.service"] in state(manager)["calls"]
    assert (snapshot(data), snapshot(destination)) == before


@pytest.mark.parametrize("point", ["after-definition-removal", "after-r0-restored"])
def test_recovery_replays_after_cleanup_interruption(case, monkeypatch, point):
    backend, *_rest, manager = case
    prepare(case)
    recover = r0_stop.recover_r0
    class Interrupted(BaseException):
        pass
    def interrupt(service):
        if point == "after-r0-restored":
            recover(service)
        raise Interrupted()
    monkeypatch.setattr(r0_stop, "recover_r0", interrupt)
    with pytest.raises(Interrupted):
        backend.recover_r1_registration()
    monkeypatch.setattr(r0_stop, "recover_r0", recover)
    result = invoke(backend.root)
    assert "Recovered the interrupted R1 registration" in result.stdout
    assert state(manager)["enabled"]
    assert not registration.journal_path(backend.service).exists()


def test_b_does_not_accept_bare_destination_as_launch_authority(case):
    backend, _, _, _, destination, manager = case
    prepare(case)
    tx = registration.BoundaryRegistration(backend.service)
    # Even manual start following a manual pointer switch is fenced without
    # C's committed install/selection records. No Runtime module is imported.
    (backend.root / "current").write_text("release1\n")
    result = subprocess.run([tx.launcher, "run", "--profile", "astrid", "--data-root", destination],
                            capture_output=True, text=True)
    assert result.returncode != 0 and "not been committed" in result.stderr
    assert not state(manager).get("r1_enabled")


@pytest.mark.parametrize("pointer", ["../escape", " leading", "v1 ", "-v1", "v" * 65])
def test_fixed_launcher_rejects_invalid_current(case, pointer):
    backend, *_ = case
    prepare(case)
    (backend.root / "current").write_text(pointer + "\n")
    result = subprocess.run([backend.root / "bin/runtime"], capture_output=True)
    assert result.returncode != 0


def test_fixed_launcher_tracks_pointer_and_uses_staged_environment_profile_and_settings(case, monkeypatch):
    backend, version, _, _, destination, _ = case
    prepare(case)
    tx = registration.BoundaryRegistration(backend.service)
    definition = tx.r1_path.read_bytes()
    selected = read_json(backend.service.selection)
    # These records simulate the future C commit. This is a launcher test,
    # never a claim of successful adoption or a production upgrade.
    write_json(backend.service.selection, {**selected, "r1_data_root": str(destination),
        "service_label": registration.R1_LINUX, "realm_id": "r0", "r1_realm_root": str(destination / "runtime/realms/r0"),
        "identity": version.document()["identity"]})
    write_json(backend.root / "install.json", {"owner": registration.R0_MAC,
        "data_root": selected["data_root"], "r1_data_root": str(destination), "identity": version.document()["identity"]})
    write_json(backend.root / "service-env.json", {"ASTRID_WARM_IDLE_SECONDS": "42"})
    for index in (1, 2):
        if index == 2:
            folder = backend.root / "versions/release2"
            folder.mkdir()
            version = replace(version, root=folder, profile=folder / "profile.json",
                              env={**version.env, "TEST_VERSION_ENV": "version two"})
            registration.stage_runtime_launcher(version)
        # -I excludes PYTHONPATH: install no packages; substitute only the final
        # Runtime import in the generated script to observe its actual argv/env.
        runner = version.root / "bin/runtime.py"
        runner.write_text(runner.read_text().replace('from astrid_runtime.cli import main',
            'def main(args):\n    print(json.dumps({"args":args,"env":dict(os.environ)}))\n    return 0'))
        (backend.root / "current").write_text(version.root.name + "\n")
        monkeypatch.setenv("ASTRID_RUNTIME_SOURCE_MANIFEST", "/wrong/profile")
        monkeypatch.setenv("ASTRID_WORKSPACE_CREDENTIAL", "must-be-removed")
        value = json.loads(subprocess.check_output([tx.launcher, "run", "--profile", "astrid", "--data-root", destination], text=True))
        assert value["env"]["TEST_VERSION_ENV"] == ("version one" if index == 1 else "version two")
        assert value["env"]["ASTRID_RUNTIME_DATA_ROOT"] == str(destination)
        assert value["env"]["ASTRID_WARM_IDLE_SECONDS"] == "42"
        assert "ASTRID_WORKSPACE_CREDENTIAL" not in value["env"]
        assert value["args"][-2:] == ["--source-manifest", str(version.profile)]
        assert tx.r1_path.read_bytes() == definition


def test_programmatic_installer_cannot_bypass_registration_recovery(case):
    backend, _, data, *_ = case
    prepare(case)
    with pytest.raises(ValueError, match="registration needs recovery"):
        Installer(backend.root, data, backend).run("update", {"release": 1})


def test_legacy_backend_registration_cannot_overwrite_r0_after_stop(case):
    backend, version, data, *_ = case
    before = snapshot(backend.root), backend.service.path.read_bytes()
    with pytest.raises(ValueError, match="legacy registration is refused"):
        backend.register(version, data)
    assert (snapshot(backend.root), backend.service.path.read_bytes()) == before


def test_unqualified_windows_refuses_without_mutation(case):
    backend, *_ = case
    backend.service.system = "win32"
    with pytest.raises(ValueError, match="not qualified"):
        registration.BoundaryRegistration(backend.service)


@pytest.mark.parametrize("problem", ["running-r0", "missing-intent", "wrong-realm", "existing-unit"])
def test_registration_requires_bound_stop_and_realm_before_any_publication(case, problem):
    backend, version, data, realm, _, manager = case
    tx = registration.BoundaryRegistration(backend.service)
    if problem == "running-r0":
        state(manager, enabled=True, running=True)
    elif problem == "missing-intent":
        r0_stop._marker_path(backend.service).unlink()
    elif problem == "wrong-realm":
        write_json(data / "runtime/catalog.json", {"version": 1, "selected_realm_id": "foreign", "realms": []})
    else:
        tx.r1_path.write_text("unrelated entry")
    before = snapshot(data), snapshot(backend.root)
    with pytest.raises((ValueError, FileNotFoundError, FileExistsError)):
        backend.prepare_r1_registration(version, data, "r0", realm)
    assert not tx.journal.exists() and not tx.launcher.exists()
    assert (snapshot(data), snapshot(backend.root)) == before
    assert not any("disable" in call or "enable" in call for call in state(manager)["calls"])


@pytest.mark.parametrize("mode", ["--uninstall", "--doctor"])
def test_other_cli_operations_refuse_pending_registration(case, mode):
    backend, *_rest, manager = case
    prepare(case)
    state(manager, calls=[])
    result = subprocess.run([sys.executable, "-m", "astrid", "install", "--root", str(backend.root), mode],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1 and "registration needs recovery" in result.stdout
    assert not state(manager)["calls"]
    assert registration.journal_path(backend.service).exists()


@pytest.mark.parametrize("mode", ["doctor", "uninstall"])
def test_programmatic_operations_refuse_completed_selection_without_c(case, mode):
    backend, _, data, _, destination, manager = case
    selected = read_json(backend.service.selection)
    write_json(backend.service.selection, {**selected, "r1_data_root": str(destination)})
    with pytest.raises(ValueError, match="handoff is already present"):
        Installer(backend.root, data, backend).run(mode, {})
    assert not state(manager)["calls"]
