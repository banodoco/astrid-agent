"""Installer quit, refusal and recovery with the retained native R0 files.

Manager responses are simulated: these are parser/state-machine tests on Linux,
not a claim that launchd or a user systemd instance ran here.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer import copy_service, core
from astrid.installer.backend import Backend
from astrid.installer.copy_update import BUSY, JOURNAL, fingerprint
from astrid.installer.service import LABEL, Service
from tests.test_installer_copy_update import PowerLoss, installation, recover  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures/release0/native-copy-service"


@pytest.fixture(params=["linux", "macos"])
def native(request, tmp_path, monkeypatch):
    name = request.param
    originals = json.loads((FIXTURES / "SOURCE.json").read_text())
    for filename, evidence in originals.items():
        assert hashlib.sha256((FIXTURES / filename).read_bytes()).hexdigest() == evidence["sha256"]
    original_base = ("/home/runner/work/_temp/astrid-r1-direct-linux" if name == "linux"
                     else "/Users/runner/work/_temp/astrid-r1-direct-macos")
    base = tmp_path / "native"
    raw = (FIXTURES / f"{name}-unit-file-before-cleanup.txt").read_text().replace(original_base, str(base))
    capture = json.loads((FIXTURES / f"{name}-manager-state-before-cleanup.json").read_text().replace(original_base, str(base)))
    root, data, home = base / "program", base / "data", base / "home"
    if name == "macos":
        document = plistlib.loads(raw.encode())
        python, env = Path(document["ProgramArguments"][0]), document["EnvironmentVariables"]
    else:
        python = Path(re.search(r'ExecStart="([^"]+)"', raw)[1])
        env = dict(json.loads(line.removeprefix("Environment=")).split("=", 1)
                   for line in raw.splitlines() if line.startswith("Environment="))
    folder = python.parents[2]
    folder.mkdir(parents=True)
    version = core.Version(folder, python, folder / "profile.json", env)
    core.write_json(folder / "version.json", version.document())
    core.switch(root, version)
    uid = str(os.getuid())
    capture["stdout"] = capture["stdout"].replace("gui/501", f"gui/{uid}")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    service_log = Path(document["StandardErrorPath"]) if name == "macos" else base / "log"
    service = Service(root, service_log, None, home=home,
                      system="linux" if name == "linux" else "darwin")
    service.path.parent.mkdir(parents=True)
    service.path.write_text(raw)
    core.write_json(service.selection, {"owner": LABEL, "program_root": str(root), "data_root": str(data)})
    core.write_json(service.env_file, {})
    service.systemd = lambda: name == "linux"
    case = SimpleNamespace(**locals(), state="running", calls=[], before_query=None,
                           after_control=None, show=capture["stdout"])

    def run(argv):
        argv = list(map(str, argv))
        case.calls.append(argv)
        if "show" in argv or "print" in argv:
            if case.before_query:
                case.before_query()
            if name == "macos":
                if case.state == "absent":
                    raise subprocess.CalledProcessError(113, argv, output="Could not find service")
                return case.show
            result = case.show
            if case.state == "absent":
                result = result.replace("ActiveState=active", "ActiveState=inactive").replace("SubState=running", "SubState=dead")
            # These additional properties are synthetic, not part of the
            # retained four-property native capture.
            return result + "Id=astrid.service\nJob=\nDropInPaths=\nTransient=no\nNeedDaemonReload=no\n"
        if "bootout" in argv or "stop" in argv:
            case.state = "absent"
        elif "bootstrap" in argv or "start" in argv:
            case.state = "running"
        else:
            raise AssertionError(argv)
        if case.after_control:
            case.after_control()
        return ""
    service.run = run
    def forbidden(*args, **kwargs):
        raise AssertionError("numeric process control is forbidden in copy update")
    monkeypatch.setattr(copy_service.platform, "signal_process", forbidden)
    monkeypatch.setattr(copy_service.platform, "process_identity", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    return case


def controls(case):
    return [call for call in case.calls if any(word in call for word in ("stop", "bootout", "start", "bootstrap"))]


@pytest.fixture
def simulated_native_control(monkeypatch):
    """Exercise the historical controller algorithm, not native qualification.

    Production stop/recovery remain closed. Safety regressions use the real
    guard and must not request this fixture.
    """
    monkeypatch.setattr(copy_service, "require_native_copy_control", lambda: None)


@pytest.mark.usefixtures("simulated_native_control")
def test_real_r0_label_path_stop_and_restore_without_pid_control(native):
    c = native
    original = c.service.path.read_bytes()
    observed = copy_service.observe(c.service, c.data)
    copy_service.stop(c.service, c.data, observed)
    assert c.state == "absent"
    copy_service.restore(c.service, c.data, observed)
    if c.name == "macos":
        assert controls(c) == [["launchctl", "bootout", f"gui/{os.getuid()}/haus.astrid.runtime"],
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(c.service.path)]]
    else:
        assert controls(c) == [["systemctl", "--user", "stop", "astrid.service"],
            ["systemctl", "--user", "start", "astrid.service"]]
    assert c.service.path.read_bytes() == original
    copy_service.restore(c.service, c.data, observed)  # already restored: no repeated start
    assert len(controls(c)) == 2


@pytest.mark.parametrize("substitution", ["path", "label", "duplicate", "transition"])
def test_native_job_substitution_refuses_before_stop(native, substitution):
    c = native
    if substitution == "path":
        c.show = c.show.replace(str(c.service.path), str(c.service.path) + ".other")
    elif substitution == "label":
        if c.name == "macos":
            c.show = c.show.replace("haus.astrid.runtime =", "another =")
        else:
            c.show += "Id=another.service\n"
    elif substitution == "duplicate":
        c.show += ("\n\tpath = /another\n" if c.name == "macos" else "FragmentPath=/another\n")
    elif c.name == "macos":
        c.show = c.show.replace("state = running", "state = starting")
    else:
        c.show = c.show.replace("ActiveState=active", "ActiveState=activating")
    with pytest.raises(ValueError):
        copy_service.observe(c.service, c.data)
    assert controls(c) == []


@pytest.mark.parametrize("race", ["configuration", "selection", "manager", "environment"])
@pytest.mark.usefixtures("simulated_native_control")
def test_substitution_between_validation_and_stop_is_refused(native, race):
    c = native
    observed = copy_service.observe(c.service, c.data)
    if race == "configuration":
        c.service.path.write_bytes(c.service.path.read_bytes() + b"\n")
    elif race == "selection":
        core.write_json(c.service.selection, {**c.service.recorded(), "external": "edit"})
    elif race == "environment":
        core.write_json(c.service.env_file, {"ASTRID_WARM_IDLE_SECONDS": "5"})
    else:
        c.show = c.show.replace(str(c.service.path), str(c.service.path) + ".replacement")
    with pytest.raises(ValueError):
        copy_service.stop(c.service, c.data, observed)
    assert controls(c) == []


def test_replacement_during_native_query_is_refused(native):
    c = native
    def replace():
        c.service.env_file.write_text('{"ASTRID_WARM_IDLE_SECONDS":"10"}')
        c.before_query = None
    c.before_query = replace
    with pytest.raises(ValueError, match="changed during validation"):
        copy_service.observe(c.service, c.data)
    assert controls(c) == []


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_aliased_native_configuration_never_controls_job(native, alias):
    c = native
    retained = c.base / "retained"
    c.service.path.rename(retained)
    if alias == "symlink":
        c.service.path.symlink_to(retained)
    else:
        os.link(retained, c.service.path)
    with pytest.raises(ValueError):
        copy_service.observe(c.service, c.data)
    assert controls(c) == []


@pytest.mark.usefixtures("simulated_native_control")
def test_external_edit_after_stop_blocks_r0_restart(native):
    c = native
    observed = copy_service.observe(c.service, c.data)
    copy_service.stop(c.service, c.data, observed)
    c.service.env_file.write_text('{"ASTRID_WARM_IDLE_SECONDS":"10"}')
    with pytest.raises(ValueError, match="files changed"):
        copy_service.restore(c.service, c.data, observed)
    assert len(controls(c)) == 1


@pytest.mark.usefixtures("simulated_native_control")
def test_native_stop_applied_then_error_can_restore_exact_r0(native):
    c = native
    observed = copy_service.observe(c.service, c.data)
    c.after_control = lambda: (_ for _ in ()).throw(OSError("lost manager reply"))
    with pytest.raises(OSError):
        copy_service.stop(c.service, c.data, observed)
    c.after_control = None
    copy_service.restore(c.service, c.data, observed)
    assert c.state == "running" and len(controls(c)) == 2


def test_busy_preflight_is_readonly_and_precedes_any_manager_access(tmp_path, monkeypatch):
    from tests.test_installer_lifecycle import _installer_runtime, _publish_owner
    backend, data, paths, realm, boundary, manager = _installer_runtime(tmp_path, monkeypatch)
    _publish_owner(paths)
    with sqlite3.connect(realm / "realm.sqlite3") as db:
        db.execute("INSERT INTO tasks (id, run_id, capability, status, created_at, updated_at) "
                   "VALUES ('busy', 'run', 'test', 'running', 'today', 'today')")
    before = fingerprint(data)
    backend.service.run = lambda *_: (_ for _ in ()).throw(AssertionError("manager accessed before busy refusal"))
    with pytest.raises(core.RuntimeOperationError, match=BUSY) as raised:
        backend.prepare_copy_stop(data)
    assert raised.value.details["safe"] is False
    assert fingerprint(data) == before
    assert boundary.stop_calls == 0 and manager == []


def test_busy_before_stage_has_no_transaction_or_service_effects(installation):
    c = installation
    c.backend.copy_preflight = lambda *_: (_ for _ in ()).throw(core.RuntimeOperationError(BUSY, details={"safe": False}))
    c.backend.stage = lambda *_: (_ for _ in ()).throw(AssertionError("must not stage busy update"))
    with pytest.raises(core.RuntimeOperationError, match=BUSY):
        c.installer._run_copy_update(c.old, {})
    assert not (c.root / JOURNAL).exists()
    assert not Path(str(c.source) + "-r1").exists()


@pytest.mark.parametrize("phase", ["stopping", "stopped", "adopting", "adopted", "switched"])
def test_recovery_restores_r0_after_durable_quit_intent(installation, phase):
    c = installation
    calls = []
    c.backend.quit_copy_source = lambda *args: calls.append("quit")
    c.backend.restore_copy_source = lambda *args: calls.append("restore")
    original = c.installer._save_update
    def crash(transaction, step, **details):
        original(transaction, step, **details)
        if step == phase:
            raise PowerLoss()
    c.installer._save_update = crash
    before = fingerprint(c.source)
    with pytest.raises(PowerLoss):
        c.installer._run_copy_update(c.old, {})
    recover(c)
    assert calls[-1] == "restore"
    assert core.current(c.root) == c.old and fingerprint(c.source) == before
    assert core.read_json(c.root / JOURNAL)["step"] == "rolled_back"
    recover(c)
    assert calls.count("restore") == 1


def test_unavailable_receipt_contract_restores_r0_and_never_runs_adopter(installation):
    c = installation
    calls = []
    c.backend.adopt = Backend.adopt.__get__(c.backend)
    c.backend.quit_copy_source = lambda *args: calls.append("quit")
    c.backend.restore_copy_source = lambda *args: calls.append("restore")
    with pytest.raises(core.RuntimeOperationError, match="receipt adapter is installed"):
        c.installer._run_copy_update(c.old, {})
    assert calls == ["quit", "restore"] and c.runtime_calls == []
    assert core.current(c.root) == c.old
    assert "Astrid will restart to update" in c.messages
    assert not Path(str(c.source) + "-r1").exists()


@pytest.mark.parametrize("wrong", ["missing", "version", "operation", "digest"])
def test_unverified_receipt_never_reaches_switch(installation, wrong):
    c = installation
    original = c.backend.adopt
    def malformed(*args, **kwargs):
        result = original(*args, **kwargs)
        if wrong == "missing":
            result.pop("receipt")
        else:
            result["receipt"][{"version": "version", "operation": "operation_id", "digest": "sha256"}[wrong]] = "invalid"
        return result
    c.backend.adopt = malformed
    restored = []
    c.backend.restore_copy_source = lambda *args: restored.append(True)
    with pytest.raises(core.RuntimeOperationError, match="receipt reference"):
        c.installer._run_copy_update(c.old, {})
    assert core.current(c.root) == c.old and restored == [True]
    assert (Path(str(c.source) + "-r1") / "runtime/realms/realm-1/realm.sqlite3").exists()


def test_detached_preflight_avoids_sqlite_source_side_effects(tmp_path, monkeypatch):
    from tests.test_installer_lifecycle import _installer_runtime
    backend, data, paths, realm, boundary, manager = _installer_runtime(tmp_path, monkeypatch)
    before = fingerprint(data)
    backend.copy_preflight(data)
    assert fingerprint(data) == before
    assert boundary.stop_calls == 0 and manager == []


def test_source_mutation_during_preflight_capture_refuses_and_retains_uncertainty(tmp_path, monkeypatch):
    from astrid.installer import copy_preflight
    from tests.test_installer_lifecycle import _installer_runtime
    backend, data, paths, realm, boundary, manager = _installer_runtime(tmp_path, monkeypatch)
    parent = tmp_path / "preflight-captures"
    parent.mkdir()
    monkeypatch.setattr(copy_preflight.tempfile, "gettempdir", lambda: str(parent))
    capture = copy_preflight._capture
    def mutate(path, destination=None):
        result = capture(path, destination)
        if destination is not None and path.name == "realm.sqlite3":
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE accepted_after_capture (value TEXT)")
                db.execute("INSERT INTO accepted_after_capture VALUES ('keep')")
        return result
    monkeypatch.setattr(copy_preflight, "_capture", mutate)
    with pytest.raises(core.RuntimeOperationError, match="changed during the busy check"):
        backend.copy_preflight(data)
    captures = list(parent.glob("astrid-copy-preflight-*"))
    assert len(captures) == 1 and (captures[0] / "runtime/realms/realm-1/realm.sqlite3").exists()
    with sqlite3.connect(realm / "realm.sqlite3") as db:
        assert db.execute("SELECT * FROM accepted_after_capture").fetchall() == [("keep",)]
    assert boundary.stop_calls == 0 and manager == []


@pytest.mark.parametrize("realm_id", ["/outside", "../outside", "a/b", "..", ""])
def test_unsafe_realm_component_refuses_before_preflight_copy(installation, realm_id):
    c = installation
    core.write_json(c.source / "runtime/catalog.json", {"selected_realm_id": realm_id,
        "realms": [{"realm_id": realm_id, "data_root": str(c.realm)}]})
    with pytest.raises(ValueError, match="inconsistent"):
        c.backend.source_workspace(c.source)


def test_candidate_busy_on_rollback_keeps_selected_r1_and_accepted_task(installation):
    c = installation
    restored = []
    c.backend.restore_copy_source = lambda *args: restored.append(True)
    prepare = c.backend.prepare_copy_stop
    def busy(data):
        if data != c.source:
            raise core.RuntimeOperationError(BUSY, details={"safe": False})
        return prepare(data)
    c.backend.prepare_copy_stop = busy
    def failed(version, data, *args):
        with sqlite3.connect(data / "runtime/realms/realm-1/realm.sqlite3") as db:
            db.execute("INSERT INTO tasks VALUES ('accepted R1 work')")
        raise RuntimeError("readiness failed")
    c.backend.verify = failed
    with pytest.raises(core.RuntimeOperationError, match=BUSY):
        c.installer._run_copy_update(c.old, {})
    assert core.current(c.root) == c.candidate and restored == []
    assert core.read_json(c.root / JOURNAL)["step"] != "rolled_back"
    with sqlite3.connect(Path(str(c.source) + "-r1") / "runtime/realms/realm-1/realm.sqlite3") as db:
        assert db.execute("SELECT name FROM tasks").fetchall() == [("accepted R1 work",)]


def test_r0_restart_failure_remains_recoverable(installation):
    c = installation
    c.backend.adopt = Backend.adopt.__get__(c.backend)
    c.backend.restore_copy_source = lambda *args: (_ for _ in ()).throw(RuntimeError("R0 restart failed"))
    with pytest.raises(RuntimeError, match="R0 restart failed"):
        c.installer._run_copy_update(c.old, {})
    assert core.read_json(c.root / JOURNAL)["step"] == "adopting"
    c.backend.restore_copy_source = lambda *args: None
    recover(c)
    assert core.read_json(c.root / JOURNAL)["step"] == "rolled_back"


@pytest.mark.usefixtures("simulated_native_control")
def test_restore_failure_after_manager_ack_does_not_claim_recovery(native):
    c = native
    observed = copy_service.observe(c.service, c.data)
    copy_service.stop(c.service, c.data, observed)
    c.after_control = lambda: setattr(c, "state", "absent")
    with pytest.raises(ValueError, match="could not be verified"):
        copy_service.restore(c.service, c.data, observed)


def test_production_contract_refusal_precedes_stage_or_native_quit(installation):
    c = installation
    c.backend.require_copy_receipt_contract = Backend.require_copy_receipt_contract.__get__(c.backend)
    c.backend.stage = lambda *args: (_ for _ in ()).throw(AssertionError("must refuse before staging"))
    c.backend.quit_copy_source = lambda *args: (_ for _ in ()).throw(AssertionError("must not quit without qualified receipt API"))
    with pytest.raises(core.RuntimeOperationError, match="receipt contract is not installed"):
        c.installer._run_copy_update(c.old, {})
    assert core.current(c.root) == c.old
    assert not (c.root / JOURNAL).exists() and c.runtime_calls == []


def test_public_cli_busy_copy_refuses_before_claims_or_stopped_owner_check(installation, monkeypatch, capsys):
    from astrid.installer import cli, service as service_module
    c = installation
    before, marker = fingerprint(c.source), (c.root / "install.json").read_bytes()
    monkeypatch.setattr(cli, "default_program_root", lambda *_: c.root)
    monkeypatch.setattr(service_module, "install_selection", lambda *_: c.selection)
    monkeypatch.setattr(cli, "installer_lock_path", lambda *_: c.tmp_path / "installer.lock")
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: c.backend)
    c.backend.source_checkout_ownership = lambda *_: False
    c.backend.require_stopped_source = lambda *_: (_ for _ in ()).throw(AssertionError("must not require prior user quit"))
    c.backend.copy_preflight = lambda *_: (_ for _ in ()).throw(core.RuntimeOperationError(BUSY, details={"safe": False}))
    assert cli.main(["--root", str(c.root), "--update"]) == 1
    output = capsys.readouterr()
    assert BUSY in output.out + output.err
    assert (c.root / "install.json").read_bytes() == marker and fingerprint(c.source) == before
    assert not (c.root / JOURNAL).exists() and c.runtime_calls == c.stops == []
