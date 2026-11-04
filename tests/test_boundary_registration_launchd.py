"""launchd registration/rollback using the captured real R0 plist and a manager fake."""
import os
import plistlib
import signal
import subprocess
from pathlib import Path

import pytest

from astrid.installer import boundary_registration as registration
from astrid.installer import r0_stop
from astrid.installer.core import Version, read_json, write_json
from tests.test_installer_r0_entry import catalog
from tests.test_installer_r0_stop import setup_case, snapshot


@pytest.fixture
def mac_case(tmp_path, monkeypatch):
    monkeypatch.setattr(r0_stop, "_launchd_uid", lambda: 501)
    manager, realm = setup_case(tmp_path, "darwin")
    manager.log = tmp_path / "runtime.log"
    manager.selection = tmp_path / "selection.json"
    catalog(realm.parent, realm)
    root = manager.root
    old = root / "versions/r0"
    old.mkdir(parents=True)
    write_json(old / "version.json", Version(old, old / "python", old / "profile", {}, release=0).document())
    (root / "current").write_text("r0\n")
    write_json(root / "install.json", {"owner": registration.R0_MAC})
    run = manager.run
    manager.r1_loaded = False
    manager.foreign_r1 = False
    def launchctl(argv, *, timeout):
        argv = list(map(str, argv))
        target = f"gui/501/{registration.R1_MAC}"
        if argv[:2] == ["launchctl", "print-disabled"]:
            manager.calls.append(argv)
            return 'disabled services = {\n "haus.astrid.runtime" => true\n}'
        if len(argv) == 3 and argv[2] == target:
            manager.calls.append(argv)
            if argv[1] == "print":
                if not manager.r1_loaded:
                    raise subprocess.CalledProcessError(113, argv, output=f'Could not find service "{registration.R1_MAC}"')
                path = manager.path.with_name(registration.R1_MAC + ".plist")
                args = plistlib.loads(path.read_bytes())["ProgramArguments"]
                if manager.foreign_r1:
                    args[0] = "/foreign/runtime"
                return (target + " = {\n\tpath = " + str(path) + "\n\targuments = {\n"
                        + "".join("\t\t" + arg + "\n" for arg in args) + "\t}\n}\n")
            if argv[1] == "bootout":
                manager.r1_loaded = False
            return ""
        return run(argv, timeout=timeout)
    manager.run = launchctl
    r0_stop.stop_r0_and_wait(manager, realm, data_root=realm.parent)
    destination = Path(str(realm.parent) + "-r1")
    adopted = destination / "runtime/realms/r0"
    adopted.mkdir(parents=True)
    (adopted / "owner.lock").write_bytes(b"retained adopted owner lock")
    (adopted / "realm.sqlite3").write_bytes(b"retained R1 work")
    folder = root / "versions/r1"
    folder.mkdir()
    version = Version(folder, folder / "python", folder / "profile", {}, release=1)
    write_json(folder / "version.json", version.document())
    launcher = folder / "bin/runtime"
    launcher.parent.mkdir()
    launcher.write_text("staged Runtime executable; not executed by registration")
    manager.calls.clear()
    return manager, registration.BoundaryRegistration(manager), version, realm, destination


def test_launchd_disable_precedes_publication_and_recovery_unloads_r1_before_r0(mac_case, monkeypatch):
    manager, tx, version, realm, destination = mac_case
    before = snapshot(realm.parent), snapshot(destination), manager.path.read_bytes()
    publish = registration._publish
    def observe(path, content, **options):
        if path in {tx.launcher, tx.r1_path}:
            assert ["launchctl", "disable", "gui/501/haus.astrid.runtime.r1"] in manager.calls
        publish(path, content, **options)
    monkeypatch.setattr(registration, "_publish", observe)
    tx.prepare(version, realm.parent, "r0", realm)
    definition = plistlib.loads(tx.r1_path.read_bytes())
    assert definition["Label"] == registration.R1_MAC
    assert definition["ProgramArguments"][0] == str(tx.root / "bin/runtime")
    assert definition["RunAtLoad"] and definition["KeepAlive"] == {"SuccessfulExit": False}
    assert not any(call[1] in {"enable", "bootstrap", "kickstart"} for call in manager.calls)
    manager.r1_loaded = True
    manager.calls.clear()
    wait = r0_stop._wait_for_owner_lock
    def observe_wait(path, *args):
        assert not manager.r1_loaded
        assert not manager.enabled
        manager.calls.append(["R1 owner lock wait"])
        return wait(path, *args)
    monkeypatch.setattr(r0_stop, "_wait_for_owner_lock", observe_wait)
    assert tx.recover()
    assert manager.enabled and manager.loaded
    assert manager.calls.index(["launchctl", "bootout", "gui/501/haus.astrid.runtime.r1"]) < manager.calls.index(
        ["R1 owner lock wait"]) < manager.calls.index(["launchctl", "enable", "gui/501/haus.astrid.runtime"])
    assert (snapshot(realm.parent), snapshot(destination), manager.path.read_bytes()) == before
    assert not tx.journal.exists()


def test_launchd_changed_loaded_arguments_are_never_stopped(mac_case):
    manager, tx, version, realm, _ = mac_case
    tx.prepare(version, realm.parent, "r0", realm)
    manager.r1_loaded = manager.foreign_r1 = True
    manager.calls.clear()
    with pytest.raises(ValueError, match="action changed"):
        tx.recover()
    assert manager.r1_loaded and not manager.enabled and tx.journal.exists()
    assert not any(call[1] in {"disable", "bootout", "enable", "bootstrap"} for call in manager.calls)


def test_launchd_changed_r0_does_not_prevent_r1_stop(mac_case):
    manager, tx, version, realm, _ = mac_case
    tx.prepare(version, realm.parent, "r0", realm)
    manager.r1_loaded = True
    manager.path.write_text("changed R0 plist")
    with pytest.raises(ValueError, match="safely restore R0"):
        tx.recover()
    assert not manager.r1_loaded and not manager.enabled and tx.journal.exists()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX SIGKILL publication cut")
@pytest.mark.parametrize("publication", ["journal", "launcher", "definition"])
def test_launchd_sigkill_after_publication_link_replays(mac_case, publication):
    manager, tx, version, realm, destination = mac_case
    before = snapshot(realm.parent), snapshot(destination), manager.path.read_bytes()
    target = {"journal": tx.journal, "launcher": tx.launcher, "definition": tx.r1_path}[publication]
    child = os.fork()
    if child == 0:
        link = registration.os.link
        def interrupt(source, dest, **options):
            link(source, dest, **options)
            if dest == target:
                os.kill(os.getpid(), signal.SIGKILL)
        registration.os.link = interrupt
        try:
            tx.prepare(version, realm.parent, "r0", realm)
        finally:
            os._exit(99)
    _, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    assert target.lstat().st_nlink == 2
    alias = registration._publication_alias(target, read_json(tx.journal))
    manager.calls.clear()
    assert tx.recover() and not tx.recover()
    assert manager.enabled and manager.loaded and not manager.r1_loaded
    assert not any(path.exists() for path in (tx.journal, tx.launcher, tx.r1_path, alias,
                                             r0_stop._marker_path(manager)))
    if publication == "definition":
        assert manager.calls.index(["launchctl", "disable", "gui/501/haus.astrid.runtime.r1"]) < manager.calls.index(
            ["launchctl", "enable", "gui/501/haus.astrid.runtime"])
    assert (snapshot(realm.parent), snapshot(destination), manager.path.read_bytes()) == before
