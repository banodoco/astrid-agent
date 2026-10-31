"""Stopped Pinokio updater custody keeps the kernel lock through activation."""

import contextlib
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from astrid.installer.app_update import locked
from astrid.installer.core import Version, switch, write_json
from astrid.installer.service import installer_lock_path


def _fixture(tmp_path, *, step="switched", selected="candidate"):
    root = tmp_path / "app" / "astrid"
    data = tmp_path / "workspace"
    home = tmp_path / "home"
    versions = {}
    for name, release in (("old", 1), ("candidate", 2)):
        folder = root / "versions" / name
        folder.mkdir(parents=True)
        python = folder / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        python.parent.mkdir(parents=True, exist_ok=True)
        python.touch()
        profile = folder / "profile.json"
        profile.write_text("{}\n", encoding="utf-8")
        version = Version(folder, python, profile, {}, release, "pinokio")
        write_json(folder / "version.json", version.document())
        versions[name] = version
    switch(root, versions[selected])
    write_json(root / "install.json", {
        "owner": "haus.astrid.runtime", "managed_by": "pinokio", "data_root": str(data),
    })
    write_json(root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "a" * 32,
        "transaction_kind": "app", "step": step, "data_root": str(data),
        "realm": {"realm_id": "realm", "realm_root": str(data / "realm")},
        "old": versions["old"].document(),
        "candidate": versions["candidate"].document(),
    })
    backend = SimpleNamespace(service=SimpleNamespace(home=home, system=sys.platform))
    return root, data, home, backend, versions


def _assert_lock_free(path):
    from astrid.installer.app_update import locked

    with locked(path):
        pass


@contextlib.contextmanager
def _custody(root, backend):
    # Production update owns this operation lock while it arms activation.
    with locked(root / ".app-update.lock"):
        from astrid.installer.custody import stopped_custody

        with stopped_custody(root, backend) as custody:
            yield custody


def test_updater_close_keeps_custody_until_attached_runtime_closes(tmp_path):
    from astrid.installer.custody import attach

    root, data, home, backend, versions = _fixture(tmp_path)
    lock_path = installer_lock_path(home, sys.platform)
    with _custody(root, backend) as custody:
        assert custody is not None
        ticket = custody.arm(versions["candidate"], data)
        attached = attach(ticket, root, versions["candidate"], data)
        custody.close()
        with pytest.raises(ValueError, match="lock"):
            _assert_lock_free(lock_path)
        attached.close()
    _assert_lock_free(lock_path)


@pytest.mark.parametrize("step,selected", [("switched", "candidate"),
                                            ("rollback_selected", "old")])
def test_custody_arm_and_attach_cover_candidate_and_rollback(tmp_path, step, selected):
    from astrid.installer.custody import attach

    root, data, _home, backend, versions = _fixture(tmp_path, step=step, selected=selected)
    with _custody(root, backend) as custody:
        assert custody is not None
        ticket = custody.arm(versions[selected], data)
        attached = attach(ticket, root, versions[selected], data)
        attached.close()


@pytest.mark.parametrize("field", ["token", "binding"])
def test_attach_rejects_tampered_ticket(tmp_path, field):
    from astrid.installer.custody import attach

    root, data, _home, backend, versions = _fixture(tmp_path)
    with _custody(root, backend) as custody:
        ticket = custody.arm(versions["candidate"], data)
        changed = dict(ticket)
        changed[field] = "tampered"
        with pytest.raises(ValueError):
            attach(changed, root, versions["candidate"], data)


@pytest.mark.parametrize("wrong", ["root", "version", "data"])
def test_attach_rejects_wrong_launch_binding_and_does_not_consume_valid_ticket(tmp_path, wrong):
    from astrid.installer.custody import attach

    root, data, _home, backend, versions = _fixture(tmp_path)
    with _custody(root, backend) as custody:
        ticket = custody.arm(versions["candidate"], data)
        bad_root, bad_version, bad_data = root, versions["candidate"], data
        if wrong == "root":
            bad_root = tmp_path / "other" / "astrid"
        elif wrong == "version":
            bad_version = versions["old"]
        else:
            bad_data = tmp_path / "other-workspace"
        with pytest.raises(ValueError):
            attach(ticket, bad_root, bad_version, bad_data)
        attached = attach(ticket, root, versions["candidate"], data)
        attached.close()


def test_attach_ticket_is_single_use(tmp_path):
    from astrid.installer.custody import attach

    root, data, _home, backend, versions = _fixture(tmp_path)
    with _custody(root, backend) as custody:
        ticket = custody.arm(versions["candidate"], data)
        attached = attach(ticket, root, versions["candidate"], data)
        with pytest.raises(ValueError):
            attach(ticket, root, versions["candidate"], data)
        attached.close()


def test_stopped_custody_yields_none_when_foreground_owns_lifetime_lock(tmp_path):
    from runtime_protocol import platform

    from astrid.installer.custody import stopped_custody

    root, _data, home, backend, _versions = _fixture(tmp_path)
    path = installer_lock_path(home, sys.platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as owner:
        platform.lock_file(owner.fileno(), blocking=False)
        with stopped_custody(root, backend) as custody:
            assert custody is None
        platform.unlock_file(owner.fileno())


def test_broker_death_closes_attached_runtime_connection(tmp_path):
    from astrid.installer.custody import attach

    root, data, _home, backend, versions = _fixture(tmp_path)
    with _custody(root, backend) as custody:
        ticket = custody.arm(versions["candidate"], data)
        attached = attach(ticket, root, versions["candidate"], data)
        attached.settimeout(5)
        custody.process.terminate()
        custody.process.wait(timeout=5)
        assert attached.recv(1) == b""
        attached.close()


def test_updater_process_exit_releases_custody_lock(tmp_path):
    """A dead updater control connection must let the broker release custody."""

    root, data, home, _backend, versions = _fixture(tmp_path)
    lock_path = installer_lock_path(home, sys.platform)
    ready = tmp_path / "updater-ready"
    script = r'''
import sys, time
from pathlib import Path
from types import SimpleNamespace
from astrid.installer.custody import stopped_custody
root, home, ready = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
backend = SimpleNamespace(service=SimpleNamespace(home=home, system=sys.platform))
with stopped_custody(root, backend) as custody:
    assert custody is not None
    ready.write_text("ready")
    time.sleep(60)
'''
    child = subprocess.Popen([sys.executable, "-c", script, str(root), str(home), str(ready)])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "updater subprocess did not acquire custody"
        with pytest.raises(ValueError, match="lock"):
            _assert_lock_free(lock_path)
    finally:
        child.terminate()
        child.wait(timeout=10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            _assert_lock_free(lock_path)
            break
        except ValueError:
            time.sleep(0.05)
    else:
        pytest.fail("custody broker retained the lifetime lock after updater exit")


@pytest.mark.parametrize("step,selected", [("switched", "candidate"), ("rollback_selected", "old")])
def test_stale_activation_phase_without_operation_lock_refuses_start(tmp_path, step, selected):
    from astrid.installer.foreground import launch

    root, _data, _home, _backend, _versions = _fixture(tmp_path, step=step, selected=selected)
    with pytest.raises(ValueError, match="^Choose Update to finish$"):
        launch(root, [])


def test_actual_runtime_keeper_exits_if_lock_custodian_dies(tmp_path):
    import json

    root, data, _home, backend, versions = _fixture(tmp_path)
    script = '''
import json,sys,time
from pathlib import Path
from astrid.installer.core import Version,read_json
from astrid.installer.custody import attach,keeper
root,data=Path(sys.argv[1]),Path(sys.argv[2])
ticket=json.loads(sys.stdin.readline())
version=Version.load(read_json(root/'versions'/'candidate'/'version.json'))
connection=attach(ticket,root,version,data)
lease=keeper(connection.fileno())
print('ready',flush=True)
time.sleep(30)
'''
    child = None
    with _custody(root, backend) as custody:
        ticket = custody.arm(versions["candidate"], data)
        try:
            child = subprocess.Popen([sys.executable, "-c", script, str(root), str(data)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True)
            child.stdin.write(json.dumps(ticket) + "\n")
            child.stdin.flush()
            assert child.stdout.readline().strip() == "ready"
            custody.process.terminate()
            custody.process.wait(timeout=5)
            assert child.wait(timeout=5) == 1
        finally:
            if child is not None:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)
                for stream in (child.stdin, child.stdout, child.stderr):
                    stream.close()
