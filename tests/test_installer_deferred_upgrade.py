"""Portable C1/C2 continuation tests, never native or served-origin proof.

Most cases use the existing real SQLite C1 fixture and explicit candidate/
backend doubles. The last case executes the real retained controller in a new
interpreter with complete copied product packages, and proves busy refusal.
Those source copies are still not a served or release-qualified installation.
"""
from pathlib import Path
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
import venv

import pytest

from astrid.installer import deferred
from astrid.installer.app_update import locked
from astrid.installer.core import Installer, RuntimeOperationError, current, read_json, switch, write_json
from astrid.installer.service import Service, command, installer_lock_path
from astrid_runtime.bootstrap import BootstrapError
from tests.test_installer_cli_recovery import _valid_cli_manifest
from tests.test_installer_systemd_upgrade import cold_candidate, upgrade, watched


@pytest.fixture
def pending_case(cold_candidate, monkeypatch, tmp_path):
    backend, old, candidate, data, paths, realm, boundary, calls, _bus, _transaction = cold_candidate
    (backend.root / "update-transaction.json").unlink()
    backend.service.path.unlink()
    boundary.live = True
    write_json(backend.service.selection, {**backend.service.recorded(),
               "foreground": {"pid": 123, "birth_id": "birth"}})
    with sqlite3.connect(realm / "realm.sqlite3") as db:
        db.execute("INSERT INTO tasks VALUES ('pending-work','render','queued','now')")
    manifest = _valid_cli_manifest(16)
    installer = Installer(backend.root, data, backend, say=lambda *_: None)
    original_inputs = deferred.execution_inputs
    code = {}
    for name, item in original_inputs().items():
        path = candidate.root / "controller-inputs" / (name + ".py")
        path.parent.mkdir(exist_ok=True)
        shutil.copyfile(item["path"], path)
        code[name] = {"path": str(path), "sha256": item["sha256"]}
    def stage(*_):
        calls.append("stage")
        return candidate
    monkeypatch.setattr(backend, "stage", stage)
    monkeypatch.setattr(backend, "validate_candidate", lambda *_: calls.append("validate-candidate"))
    monkeypatch.setattr(backend, "run", lambda *_a, **_kw: json.dumps({"format": deferred.ABI, "code": code}))
    monkeypatch.setattr(backend, "has_workspace", lambda *_: True)
    monkeypatch.setattr("astrid.installer.core.schema_unchanged", lambda *_: True)
    monkeypatch.setattr(backend, "sync_skills", lambda *_: {})
    for name in ("register", "start", "verify", "finish"):
        def action(*_args, label=name, **_kwargs):
            assert not boundary.live
            calls.append(label)
        monkeypatch.setattr(backend, name, action)
    # Unit calls run from the complete checkout. Explicitly compare the exact
    # source digests while substituting only the candidate execution location;
    # separate tests retain production origin checks and a fresh interpreter.
    def portable_execution(binding, version):
        assert version == candidate
        assert {name: item["sha256"] for name, item in binding["code"].items()} == {
            name: item["sha256"] for name, item in original_inputs().items()}
    real_execution = deferred._validate_execution
    monkeypatch.setattr(deferred, "_validate_execution", portable_execution)
    lock = tmp_path / "installer.lock"
    backend.service.activation_lock_path = lock
    lock.touch()
    (backend.root / ".app-update.lock").touch()
    def accept():
        with locked(lock), locked(backend.root / ".app-update.lock"):
            return deferred.prepare(installer, old, manifest)
    def resume():
        with locked(lock), locked(backend.root / ".app-update.lock"):
            return deferred.resume_locked(backend, read_json(backend.root / "update-transaction.json"))
    return backend, old, candidate, data, paths, realm, boundary, calls, manifest, accept, resume, real_execution


def test_deferred_request_survives_controller_exit_and_continues_existing_c2(pending_case):
    backend, old, candidate, data, paths, realm, boundary, calls, manifest, accept, resume, _real = pending_case
    selection = backend.service.selection.read_bytes()
    workspace = watched(backend, data)
    assert accept() is True
    journal = read_json(backend.root / "update-transaction.json")
    assert journal["step"] == "deferred" and journal["deferred_update"]["manifest"] == manifest
    assert current(backend.root) == old and backend.service.selection.read_bytes() == selection
    assert paths.discovery_path.exists() and boundary.live
    assert (backend.root / "run_astrid.py").is_file()
    assert all(Path(path).read_bytes() == content for path, content in workspace.items())
    assert calls == ["stage", "validate-candidate"]
    # A new Request/Installer is built by resume; no in-memory admission survives.
    boundary.live = False
    calls.clear()
    resume()
    settled = read_json(backend.root / "update-transaction.json")
    assert settled["operation_id"] == journal["operation_id"] and settled["step"] == "complete"
    assert current(backend.root) == candidate and "stage" not in calls
    assert calls.index("register") < calls.index("start") < calls.index("verify") < calls.index("finish")
    assert not any(value in calls for value in ("runtime-stop", "supervised-stop", "stop", "disable", "stop_foreground"))
    with sqlite3.connect(realm / "realm.sqlite3") as db:
        assert db.execute("SELECT status FROM tasks WHERE id='pending-work'").fetchone() == ("queued",)
    assert (data / "workspace-sentinel").read_bytes() == b"user work"


@pytest.mark.parametrize("case", ["live", "busy", "reused-publication", "reused-foreground", "unknown-foreground", "new-unit", "candidate", "profile", "code", "manifest", "controller-origin"])
def test_pending_continuation_refusal_preserves_every_byte(pending_case, monkeypatch, case):
    backend, old, candidate, data, paths, realm, boundary, calls, _manifest, accept, resume, real_execution = pending_case
    assert accept()
    boundary.live = False
    if case == "live":
        boundary.live = True
    elif case == "busy":
        with sqlite3.connect(realm / "realm.sqlite3") as db:
            db.execute("UPDATE tasks SET status='running'")
    elif case == "reused-publication":
        boundary.process_birth_identity = lambda _pid: "different-owner"
    elif case in {"reused-foreground", "unknown-foreground"}:
        paths.discovery_path.unlink()
        paths.instance_lock_path.unlink()
        if case == "reused-foreground":
            boundary.process_birth_identity = lambda _pid: "reused-owner"
        else:
            def unavailable(_pid):
                raise PermissionError("identity unavailable")
            boundary.process_birth_identity = unavailable
    elif case == "new-unit":
        backend.service.path.write_text("another registration")
    elif case == "candidate":
        write_json(candidate.root / "version.json", {**candidate.document(), "extra": True})
    elif case == "profile":
        candidate.profile.write_text('{"changed":true}')
    elif case == "code":
        (candidate.root / "controller-inputs" / "core.py").write_text("# changed")
    elif case == "manifest":
        journal = read_json(backend.root / "update-transaction.json")
        journal["deferred_update"]["manifest"]["release"] = 17
        write_json(backend.root / "update-transaction.json", journal)
    else:
        monkeypatch.setattr(deferred, "_validate_execution", real_execution)
    before = watched(backend, data)
    calls.clear()
    with pytest.raises((ValueError, RuntimeOperationError, BootstrapError)):
        resume()
    assert watched(backend, data) == before and calls == [] and current(backend.root) == old


@pytest.mark.parametrize("change", ["busy", "reused", "foreground", "lock", "job"])
def test_initial_deferred_fence_refusal_never_stages_or_writes(pending_case, change):
    backend, _old, _candidate, data, paths, realm, boundary, calls, _manifest, accept, _resume, _real = pending_case
    if change == "busy":
        with sqlite3.connect(realm / "realm.sqlite3") as db:
            db.execute("UPDATE tasks SET status='running'")
    elif change == "reused":
        boundary.process_birth_identity = lambda _pid: "reused"
    elif change == "foreground":
        write_json(backend.service.selection, {**backend.service.recorded(), "foreground": {"pid": 999, "birth_id": "birth"}})
    elif change == "lock":
        paths.bootstrap_lock_path.unlink()
    else:
        marker = read_json(backend.root / "install.json")
        write_json(backend.root / "install.json", {**marker, "update_job": {"state": "active", "operation_id": "f" * 32}})
    before = watched(backend, data)
    with pytest.raises((ValueError, RuntimeOperationError, BootstrapError)):
        accept()
    assert watched(backend, data) == before and calls == []


@pytest.mark.parametrize("change", ["busy", "selection", "job", "new-owner"])
def test_final_deferred_fence_refuses_without_accepting_or_replacing_launchers(pending_case, monkeypatch, change):
    backend, old, candidate, data, paths, realm, boundary, calls, _manifest, accept, _resume, _real = pending_case
    (backend.root / "run_astrid.py").write_bytes(b"prior launcher")
    at_change = None
    def stage(*_):
        nonlocal at_change
        if change == "busy":
            with sqlite3.connect(realm / "realm.sqlite3") as db:
                db.execute("UPDATE tasks SET status='running'")
        elif change == "selection":
            write_json(backend.service.selection, {**backend.service.recorded(), "changed": True})
        elif change == "job":
            write_json(backend.root / "install.json", {**read_json(backend.root / "install.json"), "update_job": {"state": "accepted"}})
        else:
            owner = read_json(paths.discovery_path)
            owner["runtime_instance_id"] = "another-owner"
            write_json(paths.discovery_path, owner)
            write_json(paths.instance_lock_path, owner)
        at_change = watched(backend, data)
        return candidate
    monkeypatch.setattr(backend, "stage", stage)
    with pytest.raises((ValueError, RuntimeOperationError, BootstrapError)):
        accept()
    assert at_change is not None and watched(backend, data) == at_change
    assert current(backend.root) == old and not (backend.root / "update-transaction.json").exists()


def test_interruption_after_bridge_before_acceptance_keeps_old_entry(pending_case, monkeypatch):
    backend, old, _candidate, data, _paths, _realm, _boundary, _calls, _manifest, accept, _resume, _real = pending_case
    def interrupted(*_a, **_kw):
        raise KeyboardInterrupt("before journal commit")
    monkeypatch.setattr(Installer, "_save_update", interrupted)
    with pytest.raises(KeyboardInterrupt):
        accept()
    assert (backend.root / "run_astrid.py").is_file()
    assert not (backend.root / "update-transaction.json").exists()
    assert deferred.launch_continuation(backend.root) is False and current(backend.root) == old


@pytest.mark.parametrize("remove_publications", [False, True])
def test_normal_exit_during_preparation_keeps_a_usable_cold_continuation(pending_case, monkeypatch, remove_publications):
    backend, _old, candidate, _data, paths, _realm, boundary, _calls, _manifest, accept, resume, _real = pending_case
    def stage(*_):
        boundary.live = False
        if remove_publications:
            paths.discovery_path.unlink()
            paths.instance_lock_path.unlink()
        return candidate
    monkeypatch.setattr(backend, "stage", stage)
    assert accept()
    assert read_json(backend.root / "update-transaction.json")["step"] == "deferred"
    resume()
    assert current(backend.root) == candidate


def test_deferred_choices_survive_and_apply_at_c2_activation(pending_case, monkeypatch):
    backend, _old, _candidate, _data, _paths, _realm, boundary, calls, _manifest, accept, resume, _real = pending_case
    requested = {"autostart": False, "command": True, "skills": False, "skill_copies": False}
    backend.service.choices, backend.process_control = requested, "sandbox"
    assert accept()
    backend.service.choices, backend.process_control = None, None
    boundary.live = False
    monkeypatch.setattr("runtime_protocol.platform._process_control", None)
    def register(*_):
        assert backend.service.choices == requested and backend.process_control == "sandbox"
        calls.append("register")
    monkeypatch.setattr(backend, "register", register)
    resume()
    assert read_json(backend.root / "update-transaction.json")["step"] == "complete"


def test_normal_parent_start_refuses_before_spawn_or_foreground_record(pending_case, monkeypatch):
    backend, old, _candidate, data, _paths, _realm, _boundary, _calls, _manifest, accept, _resume, _real = pending_case
    assert accept()
    monkeypatch.setattr("runtime_protocol.platform.spawn_tree", lambda *_a, **_kw: pytest.fail("spawned a continuation wrapper"))
    before = watched(backend, data)
    with locked(backend.service.activation_lock_path), pytest.raises(ValueError):
        backend.service.start_foreground(old, data)
    assert watched(backend, data) == before


def test_foreign_local_update_deferral_refuses_before_storage_or_editor(pending_case, monkeypatch):
    from astrid.installer import local_update
    backend, _old, _candidate, _data, _paths, _realm, _boundary, _calls, manifest, accept, _resume, _real = pending_case
    assert accept()
    monkeypatch.setattr(local_update, "validate_app_context", lambda value: value)
    monkeypatch.setattr(local_update, "_finish_storage_move", lambda *_: pytest.fail("storage move before admission"))
    with pytest.raises(local_update.LocalUpdateStepError, match="normal launcher"):
        local_update._execute_locked({"action": "update", "manifest": manifest, "app": {"manifest": manifest}},
                                     backend.root, backend, stdin=None, stdout=None)


@pytest.mark.parametrize("step", ["staged", "stopping", "stopped"])
def test_interrupted_deferred_c2_uses_existing_recovery(pending_case, monkeypatch, step):
    backend, old, _candidate, data, _paths, _realm, boundary, calls, _manifest, accept, _resume, _real = pending_case
    assert accept()
    transaction = read_json(backend.root / "update-transaction.json")
    transaction["step"] = step
    write_json(backend.root / "update-transaction.json", transaction)
    boundary.live = False
    installer = Installer(backend.root, data, backend, say=lambda *_: None)
    monkeypatch.setattr(installer, "_activate", lambda *_: calls.append("restore-old"))
    installer._recover_update()
    result = read_json(backend.root / "update-transaction.json")
    assert result["operation_id"] == transaction["operation_id"]
    assert result["step"] == ("complete" if step == "staged" else "rolled_back")
    assert current(backend.root) == old and "runtime-stop" not in calls


def test_staged_deferred_recovery_still_refuses_live_without_journal_write(pending_case):
    backend, _old, _candidate, data, _paths, _realm, _boundary, _calls, _manifest, accept, _resume, _real = pending_case
    assert accept()
    transaction = read_json(backend.root / "update-transaction.json")
    transaction["step"] = "staged"
    write_json(backend.root / "update-transaction.json", transaction)
    before = watched(backend, data)
    with pytest.raises((ValueError, RuntimeOperationError, BootstrapError)):
        Installer(backend.root, data, backend)._recover_update()
    assert watched(backend, data) == before


@pytest.mark.parametrize("mode", ["install", "update"])
def test_public_cli_accepts_deferred_request_before_sweep_and_reports_it(pending_case, monkeypatch, tmp_path, capsys, mode):
    from astrid.installer import cli
    backend, old, _candidate, data, _paths, _realm, _boundary, _calls, manifest, _accept, _resume, _real = pending_case
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(cli, "installer_lock_path", lambda *_: backend.service.activation_lock_path)
    monkeypatch.setattr("runtime_protocol.platform.log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(backend, "source_checkout_ownership", lambda *_: False)
    monkeypatch.setattr(cli.update_job, "sweep_owned_jobs", lambda *_a, **_kw: pytest.fail("swept pending work"))
    manifest_path = tmp_path / "manifest.json"
    write_json(manifest_path, manifest)
    argv = ["--root", str(backend.root), "--yes", "--manifest", str(manifest_path)]
    if mode == "update":
        argv.append("--update")
    assert cli.main(argv) == 3
    text = capsys.readouterr().out
    assert "did NOT update" in text and "prepared update" in text
    assert "nothing was changed" not in text.lower() and "bug in Astrid" not in text
    assert read_json(backend.root / "update-transaction.json")["step"] == "deferred"
    assert current(backend.root) == old


def test_normal_launcher_hands_off_fixed_controller_and_preserves_terminal_arguments(pending_case, monkeypatch):
    backend, _old, candidate, _data, _paths, _realm, _boundary, _calls, _manifest, accept, _resume, _real = pending_case
    assert accept()
    operation = read_json(backend.root / "update-transaction.json")["operation_id"]
    def controller_run(argv, **kwargs):
        assert argv == [str(candidate.python), "-B", "-X", "utf8", "-I", "-m", "astrid.installer.deferred", "--resume", str(backend.root), operation]
        assert "PYTHONPATH" not in kwargs["env"]
        switch(backend.root, candidate)
        transaction = read_json(backend.root / "update-transaction.json")
        write_json(backend.root / "update-transaction.json", {**transaction, "step": "complete"})
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(subprocess, "run", controller_run)
    class Replaced(BaseException):
        pass
    def replace_process(argv, env):
        assert argv == [str(candidate.python), "-X", "utf8", "-I", "-m", "astrid.omp_agent", "hello", "a spaced argument"]
        assert env["ASTRID_INSTALL_PROGRAM_ROOT"] == str(backend.root)
        raise Replaced
    # Execute the actual embedded bridge and runner; replace only final exec
    # and the controller process boundary for this portable argv/order test.
    source = (backend.root / "run_astrid.py").read_text()
    start = source.index("import json, os, re, sys\nfrom pathlib import Path", source.index("def launch_continuation"))
    namespace = {"__file__": str(backend.root / "run_astrid.py"), "replace_process": replace_process}
    exec(source[source.index("def launch_continuation"):start], namespace)
    monkeypatch.setattr(sys, "argv", [str(backend.root / "run_astrid.py"), "hello", "a spaced argument"])
    with pytest.raises(Replaced):
        exec(source[start:], namespace)


@pytest.mark.parametrize("settled", ["complete", "rolled_back"])
def test_public_cli_handoff_releases_lock_and_binds_retained_target(pending_case, monkeypatch, tmp_path, settled):
    from astrid.installer import cli
    backend, _old, _candidate, _data, _paths, _realm, _boundary, _calls, _manifest, accept, _resume, _real = pending_case
    assert accept()
    monkeypatch.setattr(cli, "Backend", lambda *_a, **_kw: backend)
    monkeypatch.setattr(cli, "installer_lock_path", lambda *_: backend.service.activation_lock_path)
    monkeypatch.setattr("runtime_protocol.platform.log_dir", lambda: tmp_path / "logs")
    def handoff(root):
        with locked(backend.service.activation_lock_path):
            transaction = read_json(root / "update-transaction.json")
            write_json(root / "update-transaction.json", {**transaction, "step": settled})
        return True
    monkeypatch.setattr(deferred, "launch_continuation", handoff)
    def finish(run, **_kwargs):
        assert run.target == 16
        assert run.reason == ("recovered-previous-version" if settled == "rolled_back" else None)
        return 77
    monkeypatch.setattr(cli._Run, "finish", finish)
    assert cli.main(["--root", str(backend.root), "--update", "--yes"]) == 77


@pytest.mark.parametrize("option", ["--update", "--uninstall", "--doctor"])
def test_pending_terminal_install_never_imports_old_r15_installer(pending_case, monkeypatch, option):
    from astrid.installer.launcher import _RUNNER
    backend, _old, _candidate, data, _paths, _realm, _boundary, _calls, _manifest, accept, _resume, _real = pending_case
    assert accept()
    before = watched(backend, data)
    monkeypatch.setattr(sys, "argv", ["run_astrid.py", "install", option])
    def forbidden(*_a, **_kw):
        pytest.fail("old installer or implicit continuation was executed")
    with pytest.raises(SystemExit, match="prepared Astrid update is pending"):
        exec(_RUNNER, {"__file__": str(backend.root / "run_astrid.py"),
                       "launch_continuation": forbidden, "replace_process": forbidden})
    assert watched(backend, data) == before


def test_admitted_c2_activation_does_not_reenter_continuation(pending_case, monkeypatch):
    from types import ModuleType
    backend, _old, candidate, data, _paths, _realm, _boundary, _calls, _manifest, accept, _resume, _real = pending_case
    assert accept()
    switch(backend.root, candidate)
    transaction = read_json(backend.root / "update-transaction.json")
    transaction.update(step="switched", transaction_context={"activation_lock_path": str(backend.service.activation_lock_path)})
    write_json(backend.root / "update-transaction.json", transaction)
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: pytest.fail("recursive continuation"))
    runtime = ModuleType("astrid_runtime.cli")
    invoked = []
    def main(argv, *, managed_launch=None):
        invoked.append((argv, managed_launch))
        return 0
    runtime.main = main
    monkeypatch.setitem(sys.modules, "astrid_runtime.cli", runtime)
    for key in candidate.env:
        monkeypatch.setenv(key, os.environ.get(key, "fixture-before"))
    script = command(candidate, data, expected_lock=backend.service.activation_lock_path)[-1]
    with locked(backend.service.activation_lock_path), pytest.raises(SystemExit) as result:
        exec(compile(script, "<portable managed activation>", "exec"), {"__name__": "__main__"})
    assert result.value.code == 0 and len(invoked) == 1
    assert invoked[0][1]["profile"] == str(candidate.profile)


@pytest.mark.parametrize("entry", ["terminal", "direct-command"])
def test_fresh_normal_launcher_executes_real_retained_controller_and_refuses_busy(pending_case, monkeypatch, tmp_path, entry):
    import astrid
    import astrid_runtime
    import runtime_protocol
    from astrid.installer.backend import Backend
    backend, old, candidate, data, paths, realm, boundary, _calls, _manifest, accept, _resume, real_execution = pending_case
    # A complete product-package copy, with genuine dependency packages from
    # this test environment. No product module or C1 implementation is stubbed
    # in the child. This deliberately remains a source-fixture origin.
    candidate.python.unlink()
    venv.EnvBuilder(with_pip=False).create(candidate.root / "py")
    library = candidate.root / "py" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    for package in (astrid, astrid_runtime, runtime_protocol):
        shutil.copytree(Path(package.__file__).parent, library / package.__name__, ignore=shutil.ignore_patterns("__pycache__"))
    (library / "fixture-dependencies.pth").write_text(sysconfig.get_path("purelib") + "\n")
    monkeypatch.setattr(backend, "run", Backend.run.__get__(backend))
    # Match the real child's ordinary per-user Service path; no manager/unit is
    # created. The original unit test selection was a custom temporary path.
    for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    record = backend.service.recorded()
    backend.service = Service(backend.root, backend.log, backend.run, home=Path.home(), system="linux")
    write_json(backend.service.selection, record)
    backend.service.activation_lock_path = installer_lock_path(Path.home(), "linux")
    backend.service.activation_lock_path.touch()
    assert accept()
    monkeypatch.setattr(deferred, "_validate_execution", real_execution)
    boundary.live = False
    paths.discovery_path.unlink()
    paths.instance_lock_path.unlink()
    with sqlite3.connect(realm / "realm.sqlite3") as db:
        db.execute("UPDATE tasks SET status='running'")
    before = watched(backend, data)
    argv = ([str(old.python), "-X", "utf8", "-I", str(backend.root / "run_astrid.py"), "hello"]
            if entry == "terminal" else command(old, data, expected_lock=backend.service.activation_lock_path))
    result = subprocess.run(argv, text=True, capture_output=True, timeout=30)
    assert result.returncode != 0 and "Astrid is busy" in result.stderr
    assert str(library / "astrid" / "installer" / "deferred.py") in result.stderr
    assert watched(backend, data) == before and current(backend.root) == old
