"""Preparatory refusal regressions derived from Astra's eight counterexamples.

Production guards stay enabled. No native manager/process control is executed;
manager responses are simulated, but SQLite, snapshots and held locks are real.
"""
from contextlib import ExitStack
import fcntl
import plistlib
import sqlite3
from pathlib import Path

import pytest

from astrid.installer import copy_service, core, copy_update
from astrid.installer.backend import Backend
from astrid.installer.copy_update import JOURNAL, fingerprint
from tests.test_installer_copy_update import installation, PowerLoss, recover  # noqa: F401
from tests.test_installer_copy_quit import native, controls  # noqa: F401


def _production_recovery(c):
    c.backend.require_copy_recovery_contract = Backend.require_copy_recovery_contract.__get__(c.backend)


def _interrupted(c):
    """Use only the explicit future-adapter fixture to manufacture stale state."""
    save = c.installer._save_update
    def crash(tx, phase, **details):
        save(tx, phase, **details)
        if phase == "switched":
            raise PowerLoss()
    c.installer._save_update = crash
    with pytest.raises(PowerLoss):
        c.installer._run_copy_update(c.old, {})
    return Path(str(c.source) + "-r1")


def _state(c, target):
    # Exact bytes include selection, journal, launchers and configuration;
    # fingerprints also cover all data content/metadata except atime.
    paths = [c.root / JOURNAL, c.root / "current", *copy_update._saved_paths(c.backend)]
    return ({str(p): p.read_bytes() if p.exists() else None for p in paths},
            fingerprint(c.source), fingerprint(target))


def _forbid_effects(c):
    def forbidden(*args, **kwargs):
        raise AssertionError("unqualified recovery/control must refuse before side effects")
    for name in ("stop", "start", "adopt", "runtime", "prepare_copy_stop", "quit_copy_source", "restore_copy_source"):
        setattr(c.backend, name, forbidden)
    c.backend.service.run = forbidden
    c.backend._discovery_owner_state = forbidden
    return forbidden


def test_late_busy_task_retained_without_native_stop(native):
    """Original late-admission probe: the advisory preflight is not a fence."""
    from runtime_protocol.store import RealmStore
    c = native
    realm = c.data / "runtime/realms/realm-1"
    RealmStore.initialize(realm, realm_id="realm-1").close()
    core.write_json(c.data / "runtime/catalog.json", {"selected_realm_id": "realm-1", "realms": [
        {"realm_id": "realm-1", "data_root": str(realm)}]})
    backend = Backend(c.root, c.base / "install.log", c.base / "uv")
    backend.service = c.service
    expected = backend.prepare_copy_stop(c.data)
    backend.copy_preflight(c.data)
    # Accepted after both detached observations. Even a receipt gate override
    # cannot authorize native stop of this newly busy owner.
    backend.require_copy_receipt_contract = lambda: None
    with sqlite3.connect(realm / "realm.sqlite3") as db:
        db.execute("INSERT INTO tasks (id, run_id, capability, status, created_at, updated_at) "
                   "VALUES ('late', 'run', 'test', 'running', 'today', 'today')")
    before = fingerprint(c.data)
    calls = list(c.calls)
    with pytest.raises(core.RuntimeOperationError, match="native copy control is unavailable"):
        backend.quit_copy_source(c.data, expected)
    assert c.calls == calls and controls(c) == [] and c.state == "running"
    assert fingerprint(c.data) == before
    with sqlite3.connect(realm / "realm.sqlite3") as db:
        assert db.execute("SELECT status FROM tasks WHERE id='late'").fetchone() == ("running",)


def test_manager_replacement_has_no_control_authority(native):
    c = native
    expected = copy_service.observe(c.service, c.data)
    original = c.service.run
    stopped_jobs = []
    def replacement(argv):
        if "stop" in argv or "bootout" in argv:
            stopped_jobs.append("replacement job")
        return original(argv)
    c.service.run = replacement
    calls = list(c.calls)
    with pytest.raises(core.RuntimeOperationError, match="native copy control is unavailable"):
        copy_service.stop(c.service, c.data, expected)
    assert stopped_jobs == [] and controls(c) == [] and c.calls == calls


def test_pre_activation_live_destination_recovery_refuses(installation):
    c = installation
    target = _interrupted(c)
    core.write_json(target / "runtime/discovery.json", {"pid": 123456, "process_birth_id": "live"})
    c.backend.require_copy_receipt_contract = Backend.require_copy_receipt_contract.__get__(c.backend)
    _production_recovery(c)
    _forbid_effects(c)
    with (target / "runtime/realms/realm-1/owner.lock").open("rb") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = _state(c, target)
        with pytest.raises(core.RuntimeOperationError, match="copy update recovery is unavailable"):
            recover(c)
        assert _state(c, target) == before
    assert core.current(c.root) == c.candidate
    assert core.read_json(c.root / JOURNAL)["step"] == "switched"


@pytest.mark.parametrize("entry", ["direct", "backend"])
@pytest.mark.parametrize("state", ["running", "absent"])
def test_restart_with_source_owner_always_refuses(native, entry, state):
    c = native
    expected = copy_service.observe(c.service, c.data)
    c.state = state
    if state == "absent":
        # Also forbid the historical no-op fast path from bypassing the gate.
        expected["state"] = "absent"
    realm = c.data / "runtime/realms/existing"
    realm.mkdir(parents=True)
    core.write_json(c.data / "runtime/catalog.json", {"selected_realm_id": "existing", "realms": [
        {"realm_id": "existing", "data_root": str(realm)}]})
    (realm / "owner.lock").touch()
    core.write_json(c.data / "runtime/discovery.json", {"pid": 123456, "process_birth_id": "live"})
    backend = Backend(c.root, c.base / "install.log", c.base / "uv")
    backend.service = c.service
    restore = (lambda: copy_service.restore(c.service, c.data, expected)) if entry == "direct" else (
        lambda: backend.restore_copy_source(c.data, expected))
    with (realm / "owner.lock").open("rb") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before, calls = fingerprint(c.data), list(c.calls)
        with pytest.raises(core.RuntimeOperationError, match="native copy control is unavailable"):
            restore()
        assert fingerprint(c.data) == before and c.calls == calls and controls(c) == []


@pytest.mark.parametrize("phase", ["staged", "stopping", "stopped", "adopting", "adopted", "switching", "switched", "activating", "recovering"])
@pytest.mark.parametrize("selected", ["old", "candidate"])
@pytest.mark.parametrize("owner", ["none", "source", "destination", "unknown"])
def test_all_stale_copy_phases_preserve_exact_state(installation, phase, selected, owner):
    c = installation
    target = _interrupted(c)
    tx = core.read_json(c.root / JOURNAL)
    tx.update(step=phase, activation_intent=phase in {"activating", "recovering"},
              candidate_stopped=phase == "recovering")
    core.write_json(c.root / JOURNAL, tx)
    core.switch(c.root, c.old if selected == "old" else c.candidate)
    if owner == "unknown":
        core.write_json(target / "runtime/discovery.json", {"unknown": True})
    # Even a future receipt capability must not enable stale recovery.
    c.backend.require_copy_receipt_contract = lambda: None
    _production_recovery(c)
    _forbid_effects(c)
    with ExitStack() as stack:
        if owner in {"source", "destination"}:
            data = c.source if owner == "source" else target
            stream = stack.enter_context((data / "runtime/realms/realm-1/owner.lock").open("rb"))
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = _state(c, target)
        with pytest.raises(core.RuntimeOperationError, match="copy update recovery is unavailable"):
            recover(c)
        assert _state(c, target) == before


@pytest.mark.parametrize("entry", ["recover_transaction", "rollback_transaction", "rollback"])
def test_direct_recovery_entries_refuse_before_writes(installation, entry):
    c = installation
    target = _interrupted(c)
    tx = core.read_json(c.root / JOURNAL)
    _production_recovery(c)
    forbidden = _forbid_effects(c)
    before = _state(c, target)
    callbacks = dict(stop=forbidden, switch_old=forbidden, activate_old=forbidden, save=forbidden)
    with pytest.raises(core.RuntimeOperationError, match="copy update recovery is unavailable"):
        if entry == "rollback":
            copy_update.rollback(c.root, c.source, c.backend, tx, forbidden)
        else:
            function = getattr(core, entry)
            if entry == "rollback_transaction":
                function(c.root, c.source, c.backend, tx, **callbacks)
            else:
                function(c.root, c.source, c.backend, **callbacks)
    assert _state(c, target) == before


@pytest.mark.parametrize("terminal", ["complete", "rolled_back"])
def test_terminal_copy_recovery_is_read_only_noop(installation, terminal):
    c = installation
    target = _interrupted(c)
    tx = core.read_json(c.root / JOURNAL)
    tx["step"] = terminal
    core.write_json(c.root / JOURNAL, tx)
    _production_recovery(c)
    forbidden = _forbid_effects(c)
    c.backend.require_copy_recovery_contract = forbidden
    before = _state(c, target)
    recover(c)
    assert _state(c, target) == before


@pytest.mark.parametrize("mode", ["--update", "--uninstall", "--delete-data"])
def test_public_cli_stale_copy_refuses_before_selection_or_sweep(installation, monkeypatch, capsys, mode):
    from astrid.installer import cli, service as service_module
    c = installation
    target = _interrupted(c)
    _production_recovery(c)
    forbidden = _forbid_effects(c)
    monkeypatch.setattr(cli, "default_program_root", lambda *_: c.root)
    monkeypatch.setattr(service_module, "install_selection", lambda *_: c.selection)
    monkeypatch.setattr(cli, "installer_lock_path", lambda *_: c.tmp_path / "installer.lock")
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: c.backend)
    monkeypatch.setattr(cli.update_job, "sweep_owned_jobs", forbidden)
    c.backend.source_checkout_ownership = lambda *_: False
    before = _state(c, target)
    options = ["--uninstall", "--delete-data"] if mode == "--delete-data" else [mode]
    assert cli.main(["--root", str(c.root), *options]) == 1
    output = capsys.readouterr()
    assert "copy update recovery is unavailable" in output.out + output.err
    assert _state(c, target) == before


@pytest.mark.parametrize("entry", ["installer", "preflight_remove", "remove"])
@pytest.mark.parametrize("delete_data", [False, True])
def test_uninstall_cannot_erase_uncertain_copy_evidence(installation, entry, delete_data):
    c = installation
    target = _interrupted(c)
    _production_recovery(c)
    forbidden = _forbid_effects(c)
    c.backend.unregister = forbidden
    before = _state(c, target)
    with pytest.raises(core.RuntimeOperationError, match="copy update recovery is unavailable"):
        if entry == "installer":
            c.installer.run("uninstall", {}, delete_data=delete_data)
        else:
            getattr(c.backend, entry)(c.root, c.source, delete_data=delete_data)
    assert _state(c, target) == before


PLIST_CHANGES = [
    ("Program", "/different/program"), ("Program", None),
    ("BundleProgram", "different"), ("WorkingDirectory", "/different"),
    ("RootDirectory", "/different"), ("EnableGlobbing", True),
    ("MachServices", {"different": True}), ("Sockets", {"different": {}}),
    ("inetdCompatibility", {"Wait": True}), ("QueueDirectories", ["/different"]),
    ("WatchPaths", ["/different"]), ("StartInterval", 1),
    ("StartCalendarInterval", {"Minute": 1}), ("UserName", "different"),
    ("RunAtLoad", False), ("RunAtLoad", 1), ("ExitTimeOut", True),
    ("ExitTimeOut", 1), ("KeepAlive", {"SuccessfulExit": 0}),
    ("KeepAlive", {"SuccessfulExit": False, "PathState": {"/different": True}}),
    ("KeepAlive", False), ("StandardOutPath", "/different"), ("StandardErrorPath", "/different"),
]


@pytest.mark.parametrize("native", ["macos"], indirect=True)
@pytest.mark.parametrize("state", ["running", "absent"])
@pytest.mark.parametrize("key,value", PLIST_CHANGES)
def test_launchd_unsupported_semantics_refuse_before_control(native, state, key, value):
    c = native
    c.state = state
    document = plistlib.loads(c.service.path.read_bytes())
    # Even a Program equal to argv[0] is outside the supported generated form.
    document[key] = document["ProgramArguments"][0] if key == "Program" and value is None else value
    c.service.path.write_bytes(plistlib.dumps(document))
    before = c.service.path.read_bytes()
    with pytest.raises(ValueError, match="launchd"):
        copy_service.observe(c.service, c.data)
    assert controls(c) == [] and c.calls == [] and c.service.path.read_bytes() == before
    if state == "running":
        with pytest.raises(ValueError, match="launchd"):
            c.service._launchd_stop_identity(c.show, c.data)


@pytest.mark.parametrize("native", ["macos"], indirect=True)
@pytest.mark.parametrize("variable", ["DYLD_INSERT_LIBRARIES", "PYTHONPATH", "ASTRID_RUNTIME_DATA_ROOT", "ASTRID_NODE_EXECUTABLE", "ASTRID_RUNTIME_DOOR_PORT", "ASTRID_INSTALL_RELEASE", "ASTRID_INSTALL_MANAGED_BY"])
def test_launchd_environment_bound_to_same_retained_action(native, variable):
    c = native
    document = plistlib.loads(c.service.path.read_bytes())
    document["EnvironmentVariables"][variable] = "/different"
    c.service.path.write_bytes(plistlib.dumps(document))
    with pytest.raises(ValueError, match="launchd environment"):
        copy_service.observe(c.service, c.data)
    with pytest.raises(ValueError, match="launchd environment"):
        c.service._launchd_stop_identity(c.show, c.data)
    assert c.calls == [] and controls(c) == []


@pytest.mark.parametrize("native", ["macos"], indirect=True)
def test_launchd_environment_from_another_known_version_is_refused(native):
    c = native
    other = c.root / "versions/other"
    other.mkdir()
    environment = {**c.version.env, "ASTRID_NODE_EXECUTABLE": "/different/node"}
    version = core.Version(other, other / "py/bin/python", other / "profile.json", environment)
    core.write_json(other / "version.json", version.document())
    document = plistlib.loads(c.service.path.read_bytes())
    document["EnvironmentVariables"] = version.env
    c.service.path.write_bytes(plistlib.dumps(document))
    with pytest.raises(ValueError, match="launchd environment"):
        copy_service.observe(c.service, c.data)
    assert c.calls == [] and controls(c) == []


@pytest.mark.parametrize("native", ["macos"], indirect=True)
@pytest.mark.parametrize("change", ["missing", "nonstring", "partial_identity"])
def test_launchd_environment_shape_and_compatibility_are_exact(native, change):
    c = native
    document = plistlib.loads(c.service.path.read_bytes())
    environment = document["EnvironmentVariables"]
    if change == "missing":
        environment.pop("ASTRID_NODE_EXECUTABLE")
    elif change == "nonstring":
        environment["ASTRID_RUNTIME_DOOR_PORT"] = 17340
    else:
        environment["ASTRID_INSTALL_RELEASE"] = "0"
    c.service.path.write_bytes(plistlib.dumps(document))
    with pytest.raises(ValueError, match="launchd"):
        copy_service.observe(c.service, c.data)
    assert c.calls == [] and controls(c) == []


@pytest.mark.parametrize("native", ["macos"], indirect=True)
def test_retained_program_override_cannot_authorize_bootstrap(native, monkeypatch):
    c = native
    expected = copy_service.observe(c.service, c.data)
    document = plistlib.loads(c.service.path.read_bytes())
    document["Program"] = "/different/program"
    c.service.path.write_bytes(plistlib.dumps(document))
    c.state = "absent"
    # Model a stale journal that authenticated initially wrong snapshot bytes.
    expected["files"] = copy_service.files(c.service)
    calls = list(c.calls)
    with pytest.raises(core.RuntimeOperationError, match="native copy control is unavailable"):
        copy_service.restore(c.service, c.data, expected)
    assert c.calls == calls and controls(c) == []
    # Defense in depth: even the historical controller with its qualification
    # guard explicitly bypassed refuses invalid semantics before bootstrap.
    monkeypatch.setattr(copy_service, "require_native_copy_control", lambda: None)
    with pytest.raises(ValueError, match="unsupported configuration keys"):
        copy_service.restore(c.service, c.data, expected)
    assert c.calls == calls and controls(c) == []


@pytest.mark.parametrize("native", ["macos"], indirect=True)
def test_supported_current_launchd_configuration_observes_without_control(native):
    from astrid.installer.service import render
    c = native
    c.state = "absent"
    c.service.path.write_text(render("darwin", c.version, c.data, c.service.log, env_file=c.service.env_file))
    assert copy_service.observe(c.service, c.data)["state"] == "absent"
    assert controls(c) == []
