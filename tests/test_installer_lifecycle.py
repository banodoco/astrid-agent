"""Lifecycle coverage for reinstalling a kept workspace and installed doctor."""

import importlib
import json
import os
import runpy
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer.core import (
    Installer,
    RuntimeOperationError,
    VerificationError,
    Version,
    current,
    is_lifecycle_refusal,
    read_json,
    switch,
    write_json,
)
from astrid.installer.service import LABEL, install_selection
from astrid.installer.support import FAILURE_MESSAGE, HELP_MESSAGE
from astrid.sdk import storage_root


def test_runtime_commands_do_not_inject_rejected_render_startup_overrides(tmp_path, monkeypatch):
    from astrid.installer.backend import Backend

    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    backend = Backend(tmp_path, tmp_path / "install.log", Path(sys.executable))
    environment = json.loads(backend.run([
        sys.executable, "-I", "-c",
        "import json,os; print(json.dumps({key: os.environ[key] for key in "
        "('HOME','PYTHONUTF8','PYTHONIOENCODING') if key in os.environ}))",
    ]))

    assert environment["PYTHONIOENCODING"] == "utf-8"
    assert environment["HOME"] == str(tmp_path)


class LifecycleBackend:
    def __init__(self, root: Path, data: Path, *, reinstallable: bool = True):
        self.root, self.data = root, data
        self.old = None
        self.reinstallable_value = reinstallable
        self.calls: list[tuple] = []
        self.candidate = None
        self.staged_manifest = None
        self.sync_failure = False
        self.restart_required_value = False

    def source_checkout_ownership(self, _data, _version):
        return False

    def existing(self, _data):
        return self.old

    def has_workspace(self, data):
        return (data / "runtime" / "catalog.json").is_file()

    def reinstallable(self, data):
        self.calls.append(("reinstallable", data))
        return self.reinstallable_value

    def stage(self, manifest, root, _data):
        self.calls.append(("stage",))
        self.staged_manifest = manifest
        folder = root / "versions" / "candidate"
        folder.mkdir(parents=True)
        self.candidate = Version(folder, folder / "py/bin/python", folder / "profile.json", {},
                                release=manifest.get("release", 0))
        self.candidate.python.parent.mkdir(parents=True)
        self.candidate.python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        write_json(folder / "version.json", self.candidate.document())
        return self.candidate

    def validate_candidate(self, version, _data, _realm):
        self.calls.append(("validate_candidate", version))

    def attach_existing(self, version, data):
        self.calls.append(("attach", version, data))
        return {"realm_id": "kept-realm", "realm_root": str(data / "runtime/realms/kept-realm")}

    def workspace(self, *_args):
        return {"realm_id": "kept-realm", "realm_root": str(self.data / "runtime/realms/kept-realm")}

    def backup(self, *_args):
        self.calls.append(("backup",))
        return {"root": str(self.root / "backups" / "kept")}

    def upgrade(self, version, data):
        self.calls.append(("upgrade", version, data))

    def create(self, *_args):
        self.calls.append(("create",))
        return {"realm_id": "new-realm"}

    def register(self, version, data):
        self.calls.append(("register", version, data))

    def start(self, version, data):
        self.calls.append(("start", version, data))

    def verify(self, version, data, realm_id, *, timeout=90):
        self.calls.append(("verify", version, data, realm_id))

    def restart_required(self):
        self.calls.append(("restart_required",))
        return self.restart_required_value

    def sync_skills(self, version):
        self.calls.append(("sync_skills", version))
        if self.sync_failure:
            raise RuntimeError("sync failed")

    def finish(self, old, new, backup):
        self.calls.append(("finish", old, new, backup))

    def open(self):
        self.calls.append(("open",))

    def doctor(self, version, data, bundle):
        self.calls.append(("doctor", version, data, bundle))
        return bundle

    def stop(self, version, data):
        self.calls.append(("stop", version, data))

    def preflight_stop(self, data):
        self.calls.append(("preflight_stop", data))

    def unregister(self):
        self.calls.append(("unregister",))

    def remove(self, root, data, *, delete_data):
        self.calls.append(("remove", root, data, delete_data))
        return []

def test_reinstall_attaches_kept_realm_without_creating_another(tmp_path):
    root, data = tmp_path / "program", tmp_path / "data"
    catalog = data / "runtime" / "catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({"selected_realm_id": "kept-realm"}), encoding="utf-8")
    realm = data / "runtime" / "realms" / "kept-realm"
    realm.mkdir(parents=True)
    sentinel = realm / "kept.bin"
    sentinel.write_bytes(b"keep")
    backend = LifecycleBackend(root, data)

    Installer(root, data, backend, say=lambda _line: None).run("install", {})

    assert [call[0] for call in backend.calls] == [
        "reinstallable", "stage", "attach", "register", "start", "verify", "sync_skills", "finish", "open"
    ]
    assert not any(call[0] == "create" for call in backend.calls)
    assert backend.calls[5][3] == "kept-realm"
    assert sentinel.read_bytes() == b"keep"


def test_fresh_install_runs_skills_sync_after_verify(tmp_path):
    root, data = tmp_path / "program", tmp_path / "data"
    backend = LifecycleBackend(root, data)

    Installer(root, data, backend, say=lambda _line: None).run("install", {})

    names = [call[0] for call in backend.calls]
    assert names.index("verify") < names.index("sync_skills") < names.index("finish")


def test_reinstall_without_sandbox_rewrites_setting_after_sandbox_install(tmp_path):
    from runtime_protocol import process_control

    from astrid.installer.backend import Backend

    root, data = tmp_path / "program", tmp_path / "data"
    support = data / "runtime"
    support.mkdir(parents=True)
    realm = {"realm_id": "kept-realm", "realm_root": str(data / "runtime/realms/kept-realm")}

    for expected in ("sandbox", "strict"):
        backend = Backend.__new__(Backend)
        backend.process_control = expected
        calls = []
        version_root = root / "versions" / "candidate"

        def stage(_manifest, _root, _data):
            version_root.mkdir(parents=True)
            version = Version(version_root, version_root / "py/bin/python", version_root / "profile.json", {})
            version.python.parent.mkdir(parents=True)
            version.python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            write_json(version_root / "version.json", version.document())
            return version

        def runtime(_version, _data, *args, **_kwargs):
            calls.append(args)
            if args[:2] == ("workspace", "inspect"):
                return realm
            assert args[:2] == ("workspace", "attach")
            setting = args[args.index("--process-control") + 1]
            process_control.write_setting(support, setting)
            return realm

        backend.existing = lambda _data: None
        backend.has_workspace = lambda _data: True
        backend.reinstallable = lambda _data: True
        backend.stage = stage
        backend.runtime = runtime
        backend.register = lambda *_args: None
        backend.start = lambda *_args: None
        backend.verify = lambda *_args: None
        backend.sync_skills = lambda *_args: None
        backend.finish = lambda *_args: None
        backend.open = lambda: None

        Installer(root, data, backend, say=lambda _line: None).run("install", {})

        assert calls[-1][-2:] == ("--process-control", expected)
        assert process_control.read_setting(support) == expected
        (root / "current").unlink()
        shutil.rmtree(version_root)


def test_upgrade_runs_skills_sync_after_verify(tmp_path):
    root, data = tmp_path / "program", tmp_path / "data"
    old_root = root / "versions" / "old"
    old_root.mkdir(parents=True)
    old = Version(old_root, old_root / "py/bin/python", old_root / "profile.json", {})
    old.python.parent.mkdir(parents=True)
    old.python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    write_json(old_root / "version.json", old.document())
    backend = LifecycleBackend(root, data)
    backend.old = old

    Installer(root, data, backend, say=lambda _line: None).run("update", {})

    names = [call[0] for call in backend.calls]
    assert names.index("stage") < names.index("validate_candidate") < names.index("stop")
    assert names.index("stop") < names.index("backup") < names.index("upgrade")
    assert names.index("upgrade") < names.index("verify") < names.index("sync_skills") < names.index("finish")


def test_staged_journal_publication_failure_keeps_old_owner_running(tmp_path):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path)
    installer = Installer(root, data, backend, say=lambda _line: None)
    save_update = installer._save_update

    def fail_staged(transaction, step, **details):
        if step == "staged":
            raise OSError("staged journal write failed")
        save_update(transaction, step, **details)

    installer._save_update = fail_staged
    with pytest.raises(OSError, match="staged journal write failed"):
        installer.run("update", {})

    assert [call[0] for call in backend.calls] == ["stage", "validate_candidate"]
    assert current(root) == old
    assert read_json(root / "update-transaction.json") == {}


def test_service_admission_publication_precedes_first_transaction_stop(tmp_path):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path)
    backend.publish_launch_admission = lambda version, _data: backend.calls.append(
        ("publish_launch_admission", version)
    )

    Installer(root, data, backend, say=lambda _line: None).run("update", {})

    names = [call[0] for call in backend.calls]
    assert names.index("publish_launch_admission") < names.index("stop")
    assert backend.calls[names.index("publish_launch_admission")][1] == old


def test_service_admission_publication_failure_aborts_before_first_stop(tmp_path):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path)
    calls = []

    def fail_publication(version, _data):
        calls.append(("publish_launch_admission", version))
        raise OSError("service admission publication failed")

    backend.publish_launch_admission = fail_publication
    with pytest.raises(OSError, match="service admission publication failed"):
        Installer(root, data, backend, say=lambda _line: None).run("update", {})

    transaction = read_json(root / "update-transaction.json")
    assert transaction["step"] == "staged"
    assert transaction["old"] == old.document()
    assert current(root) == old
    assert calls == [("publish_launch_admission", old)]
    assert [call[0] for call in backend.calls] == ["stage", "validate_candidate"]


def test_recovery_admission_publication_failure_leaves_pending_journal_and_backup(tmp_path):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path)
    candidate = backend.stage({}, root, data)
    realm = backend.workspace(old, data)
    backup = {"root": str(root / "backups" / "retained")}
    journal = {
        "format": "astrid-install-update/v1", "operation_id": "1" * 32,
        "transaction_kind": "migration", "step": "backed_up", "data_root": str(data),
        "old": old.document(), "candidate": candidate.document(), "realm": realm,
        "backup": backup,
    }
    write_json(root / "update-transaction.json", journal)
    before = (root / "update-transaction.json").read_bytes()
    calls = []

    def fail_publication(version, _data):
        calls.append(("publish_launch_admission", version))
        raise OSError("service admission publication failed")

    backend.publish_launch_admission = fail_publication
    with pytest.raises(OSError, match="service admission publication failed"):
        Installer(root, data, backend, say=lambda _line: None)._recover_update()

    assert (root / "update-transaction.json").read_bytes() == before
    assert read_json(root / "update-transaction.json")["backup"] == backup
    assert current(root) == old
    assert calls == [("publish_launch_admission", old)]
    assert not any(call[0] in {"stop", "restore", "register", "start"} for call in backend.calls)


def test_switched_journal_publication_failure_rolls_back_selected_candidate(tmp_path):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path)
    backend.backup = lambda *_args: backend.calls.append(("backup",)) or {
        "root": str(root / "backups" / "retained")
    }
    backend.restore = lambda *_args: backend.calls.append(("restore",))
    installer = Installer(root, data, backend, say=lambda _line: None)
    save_update = installer._save_update

    def fail_switched(transaction, step, **details):
        if step == "switched":
            assert current(root) == backend.candidate
            raise OSError("switched journal write failed")
        save_update(transaction, step, **details)

    installer._save_update = fail_switched
    with pytest.raises(OSError, match="switched journal write failed"):
        installer.run("update", {})

    assert current(root) == old
    assert [call[0] for call in backend.calls] == [
        "stage", "validate_candidate", "stop", "backup", "upgrade", "stop",
        "stop", "restore", "register", "start", "verify",
    ]
    assert read_json(root / "update-transaction.json")["step"] == "rolled_back"


@pytest.mark.parametrize("category", ["busy", "timeout", "identity"])
def test_runtime_stop_report_preserves_details_and_only_lifecycle_refusal_skips_rollback(tmp_path, category):
    root, data = tmp_path / "program", tmp_path / "data"
    folder = root / "versions" / "old"
    folder.mkdir(parents=True)
    old = Version(folder, folder / "py/bin/python", folder / "profile.json", {})
    old.python.parent.mkdir(parents=True)
    old.python.write_text("python")
    write_json(folder / "version.json", old.document())
    switch(root, old)
    backend = LifecycleBackend(root, data)
    backend.old = old
    details = {"kind": category, "blockers": [{"task_id": "task-1", "state": "running"}]}

    if category == "busy":
        details["safe"] = False

    def refuse(*_):
        transaction = read_json(root / "update-transaction.json")
        assert transaction["step"] == "stopping"
        assert transaction["candidate"]["root"] == str(root / "versions" / "candidate")
        backend.calls.append(("stop", old, data))
        raise RuntimeOperationError("Astrid is busy (1 tasks running). Try again when they finish.",
                                    details=details)

    backend.stop = refuse
    installer = Installer(root, data, backend, say=lambda _line: None)
    published_steps = []
    save_update = installer._save_update

    def observe_save(transaction, step, **details):
        published_steps.append(step)
        save_update(transaction, step, **details)

    installer._save_update = observe_save

    original_refuse = backend.stop

    def assert_staged_then_refuse(*args):
        assert published_steps[:2] == ["staged", "stopping"]
        return original_refuse(*args)

    backend.stop = assert_staged_then_refuse
    with pytest.raises(RuntimeError) as raised:
        installer.run("update", {})

    if category != "busy":
        assert raised.value.__cause__.details == details
        assert current(root) == old
        assert [call[0] for call in backend.calls] == ["stage", "validate_candidate", "stop", "stop"]
        assert read_json(root / "update-transaction.json")["step"] == "stopping"
        return
    assert raised.value.details == details
    assert current(root) == old
    assert [call[0] for call in backend.calls] == ["stage", "validate_candidate", "stop"]
    transaction = read_json(root / "update-transaction.json")
    assert transaction["step"] == "complete"
    assert transaction["outcome"] == "stop_refused"


@pytest.mark.parametrize("operation", ["update", "restart-repair", "recovery"])
def test_legacy_terminal_survives_public_install_final_stop_refusal(tmp_path, monkeypatch, operation):
    from astrid.installer import launcher

    root, data = tmp_path / "program", tmp_path / "data"
    folder = root / "versions" / "old"
    legacy = {"root": str(folder), "python": sys.executable,
              "profile": str(folder / "profile.json"), "env": {}}
    write_json(folder / "version.json", legacy)
    write_json(folder / "manifest.json", {"release": 0})
    marker = {"owner": LABEL, "data_root": str(data), "preferences": {"open": False}}
    write_json(root / "install.json", marker)
    (root / "current").write_text("old\n", encoding="utf-8")
    # Generate the retained pre-X6 command, rather than pre-stamping the legacy
    # record to make the new runner accept it. Production has no such fallback.
    with monkeypatch.context() as retained:
        retained.setattr(launcher, "_RUNNER", (
            Path(__file__).parent / "fixtures" / "installer_legacy_runner.py"
        ).read_text(encoding="utf-8"))
        terminal, runner = launcher.install(root)
    retained_command = {path: path.read_bytes() for path in (terminal, runner)}
    (root / "run_runtime.py").write_text("# stale Runtime gate\n", encoding="utf-8")
    backend = LifecycleBackend(root, data)
    backend.restart_required_value = True
    release = 1 if operation == "update" else 0
    pending = {"format": "astrid-install-update/v1", "operation_id": "a" * 32,
               "transaction_kind": "migration", "data_root": str(data),
               "old": legacy, "candidate": None, "backup": None,
               "realm": backend.workspace(), "step": "stopping"}
    if operation == "recovery":
        write_json(root / "update-transaction.json", pending)
    admission = []
    stops = []

    def publish(version, workspace):
        admission.append((version, workspace))
        assert "launch_admission" in (root / "run_runtime.py").read_text(encoding="utf-8")

    def refuse(version, workspace):
        stops.append((version, workspace))
        assert admission == [(version, workspace)]
        assert read_json(folder / "version.json") == legacy
        assert read_json(root / "install.json") == marker
        if operation in {"update", "recovery"}:
            assert read_json(root / "update-transaction.json")["step"] == "stopping"
        raise RuntimeOperationError("Astrid is busy",
                                    details={"safe": False, "kind": "busy"})

    backend.publish_launch_admission = publish
    backend.stop = refuse
    handoffs = []

    class SelectedCLI(Exception):
        pass

    def execvpe(python, argv, env):
        handoffs.append((python, argv, env))
        raise SelectedCLI

    monkeypatch.setattr(os, "execvpe", execvpe)
    monkeypatch.setattr(sys, "argv", [str(runner), "projects", "list"])

    def invoke_terminal():
        with pytest.raises(SelectedCLI):
            runpy.run_path(str(runner), run_name="__main__")
        assert handoffs[-1][0] == sys.executable
        assert handoffs[-1][1] == [sys.executable, "-X", "utf8", "-I", "-m",
                                   "astrid.omp_agent", "projects", "list"]
        assert handoffs[-1][2]["ASTRID_INSTALL_PROGRAM_ROOT"] == str(root)

    invoke_terminal()
    with pytest.raises(RuntimeOperationError, match="Astrid is busy") as raised:
        Installer(root, data, backend, say=lambda _line: None).run("install", {"release": release})

    assert is_lifecycle_refusal(raised.value)
    assert len(stops) == 1
    assert (root / "current").read_text(encoding="utf-8") == "old\n"
    assert read_json(folder / "version.json") == legacy
    assert read_json(root / "install.json") == marker
    if release:
        transaction = read_json(root / "update-transaction.json")
        assert transaction["step"] == "complete"
        assert transaction["outcome"] == "stop_refused"
    elif operation == "recovery":
        assert read_json(root / "update-transaction.json") == pending
    invoke_terminal()
    assert len(handoffs) == 2
    assert all(path.read_bytes() == content for path, content in retained_command.items())


def test_post_stop_failure_runs_update_recovery(tmp_path):
    root, data = tmp_path / "program", tmp_path / "data"
    folder = root / "versions" / "old"
    folder.mkdir(parents=True)
    old = Version(folder, folder / "py/bin/python", folder / "profile.json", {})
    old.python.parent.mkdir(parents=True)
    old.python.write_text("python")
    write_json(folder / "version.json", old.document())
    switch(root, old)
    backend = LifecycleBackend(root, data)
    backend.old = old

    def partial_stop(*_):
        backend.calls.append(("stop", old, data))
        if len([call for call in backend.calls if call[0] == "stop"]) == 1:
            raise RuntimeOperationError("service manager failed after Runtime stopped",
                                        details={"cause_details": {"safe": False}})

    backend.stop = partial_stop
    with pytest.raises(RuntimeOperationError):
        Installer(root, data, backend, say=lambda _line: None).run("update", {})

    assert [call[0] for call in backend.calls] == [
        "stage", "validate_candidate", "stop", "stop", "register", "start", "verify"
    ]
    assert current(root) == old


def _installed_lifecycle_backend(tmp_path, *, release=0):
    root, data = tmp_path / "program", tmp_path / "data"
    folder = root / "versions" / "old"
    folder.mkdir(parents=True)
    old = Version(folder, folder / "py/bin/python", folder / "profile.json", {}, release=release)
    old.python.parent.mkdir(parents=True)
    old.python.write_text("python")
    write_json(folder / "version.json", old.document())
    switch(root, old)
    backend = LifecycleBackend(root, data)
    backend.old = old
    return root, data, old, backend


@pytest.mark.parametrize(
    ("installed", "candidate", "agent_sha", "runtime_sha", "updates"),
    [
        (0, None, "a", "b", False),       # absent candidate release means zero
        (0, 0, "c", "d", False),          # equal release, SHAs differ from installed pins
        (9, 10, "a", "b", True),           # numeric 9 -> 10, same SHAs as installed
        (10, 9, "b", "c", False),          # refuse a served release older than this installation
        (10, 10, "c", "d", False),         # same release never switches by SHA
    ],
)
def test_install_dispatches_by_numeric_release(installed, candidate, agent_sha, runtime_sha, updates,
                                               tmp_path):
    root, data, _old, backend = _installed_lifecycle_backend(tmp_path, release=installed)
    write_json(root / "versions/old/manifest.json", {
        "release": installed, "agent": {"sha": "a"}, "runtime": {"sha": "b"},
    })
    backend.workspace = lambda *_args: {"realm_id": "kept-realm", "realm_root": str(data / "realm")}
    manifest = {"agent": {"sha": agent_sha}, "runtime": {"sha": runtime_sha}}
    if candidate is not None:
        manifest["release"] = candidate

    if candidate is not None and candidate < installed:
        with pytest.raises(ValueError, match="This Astrid is newer than astrid.haus offers"):
            Installer(root, data, backend, say=lambda _line: None).run("install", manifest)
        assert backend.calls == []
        assert current(root).release == installed
        return

    Installer(root, data, backend, say=lambda _line: None).run("install", manifest)

    names = [call[0] for call in backend.calls]
    assert ("stage" in names) is updates
    if updates:
        assert names.index("stage") < names.index("validate_candidate") < names.index("stop")
        assert current(root).release == candidate
    else:
        assert "verify" in names and "restart_required" in names
        assert not {"stage", "stop", "backup", "upgrade", "register", "start"}.intersection(names)
        assert current(root).release == installed


@pytest.mark.parametrize("installed", [None, 0, 9, 10])
@pytest.mark.parametrize("candidate", [None, 0, 9, 10])
def test_legacy_version_record_release_dispatch_matrix(tmp_path, installed, candidate):
    effective_installed = installed or 0
    root, data, old, backend = _installed_lifecycle_backend(tmp_path, release=effective_installed)
    legacy_record = old.document()
    legacy_record.pop("identity")
    write_json(old.root / "version.json", legacy_record)
    write_json(old.root / "manifest.json", {} if installed is None else {"release": installed})
    manifest = {} if candidate is None else {"release": candidate}

    effective_candidate = candidate or 0
    if effective_candidate < effective_installed:
        with pytest.raises(ValueError, match="This Astrid is newer than astrid.haus offers"):
            Installer(root, data, backend, say=lambda _line: None).run("install", manifest)
        assert backend.calls == []
        assert current(root).release == effective_installed
        return

    Installer(root, data, backend, say=lambda _line: None).run("install", manifest)

    assert ("stage" in [call[0] for call in backend.calls]) is (effective_candidate > effective_installed)
    assert current(root).release == max(effective_candidate, effective_installed)


@pytest.mark.parametrize("health", ["healthy", "unhealthy", "restart-required"])
def test_same_release_repair_keeps_installed_version_and_never_stages(tmp_path, health):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path, release=10)
    backend.restart_required_value = health == "restart-required"
    backend.workspace = lambda *_args: {"realm_id": "kept-realm", "realm_root": str(data / "realm")}
    if health == "unhealthy":
        verification_attempts = []

        def unhealthy(version, workspace, realm_id, **_kwargs):
            backend.calls.append(("verify", version, workspace, realm_id))
            verification_attempts.append(True)
            if len(verification_attempts) == 1:
                raise VerificationError("restart required")

        backend.verify = unhealthy

    Installer(root, data, backend, say=lambda _line: None).run("install", {"release": 10})

    names = [call[0] for call in backend.calls]
    assert current(root) == old
    assert not {"stage", "backup", "upgrade", "finish"}.intersection(names)
    if health != "healthy":
        assert names.count("stop") == 1 and names.count("start") == 1
        assert names.count("register") == 1 and names.count("verify") == 2
    else:
        assert "verify" in names and "restart_required" in names
        assert not {"stop", "start", "register"}.intersection(names)


def test_explicit_update_rejects_malformed_candidate_release_before_staging(tmp_path):
    root, data, _old, backend = _installed_lifecycle_backend(tmp_path, release=10)

    with pytest.raises(ValueError, match="nonnegative integer"):
        Installer(root, data, backend, say=lambda _line: None).run("update", {"release": True})

    assert backend.calls == []


@pytest.mark.parametrize("candidate", [9, 10])
def test_explicit_update_forces_same_or_older_release_restage(tmp_path, candidate):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path, release=10)
    manifest = {"release": candidate, "agent": {"sha": "a"}, "runtime": {"sha": "b"}}

    Installer(root, data, backend, say=lambda _line: None).run("update", manifest)

    assert any(call[0] == "stage" for call in backend.calls)
    assert any(call[0] == "stop" for call in backend.calls)
    assert current(root).release == candidate


@pytest.mark.parametrize("failure", ["preparation", "busy", "rollback"])
def test_newer_release_keeps_shared_transaction_safety(tmp_path, failure):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path, release=1)
    manifest = {"release": 2}
    backend.backup = lambda *_args: {"root": str(root / "backups" / "kept")}
    backend.upgrade = lambda *_args: None

    if failure == "preparation":
        def reject_candidate(*_args):
            backend.calls.append(("validate_candidate",))
            raise ValueError("invalid candidate")

        backend.validate_candidate = reject_candidate
        expected = ValueError
        message = "invalid candidate"
    elif failure == "busy":
        def refuse_stop(*_args):
            backend.calls.append(("stop",))
            raise RuntimeOperationError("owner busy", details={"safe": False, "task_count": 1})

        backend.stop = refuse_stop
        expected = RuntimeOperationError
        message = "owner busy"
    else:
        verification_attempts = []

        def fail_candidate_once(_version, _data, _realm_id):
            verification_attempts.append(True)
            if len(verification_attempts) == 1:
                raise VerificationError("candidate did not become ready")

        backend.verify = fail_candidate_once
        backend.restore = lambda *_args: backend.calls.append(("restore",))
        expected = VerificationError
        message = "candidate did not become ready"

    with pytest.raises(expected, match=message):
        Installer(root, data, backend, say=lambda _line: None).run("install", manifest)

    assert current(root) == old
    if failure == "preparation":
        assert [call[0] for call in backend.calls] == ["stage", "validate_candidate"]
    elif failure == "busy":
        assert [call[0] for call in backend.calls] == ["stage", "validate_candidate", "stop"]
    else:
        assert read_json(root / "update-transaction.json")["step"] == "rolled_back"
        assert "restore" in [call[0] for call in backend.calls]


def test_release_comparison_reads_version_after_journal_recovery(tmp_path, monkeypatch):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path, release=3)
    recovery_calls = []

    def recover_first():
        recovery_calls.append("recovered")
        # Model recovery selecting and restoring the old owner's version record.
        restored = Version(old.root, old.python, old.profile, old.env, release=9)
        write_json(old.root / "version.json", restored.document())

    monkeypatch.setattr(Installer, "_recover_update", lambda self: recover_first())
    backend.backup = lambda *_args: {"root": str(root / "backups" / "kept")}
    backend.upgrade = lambda *_args: None
    manifest = {"release": 10}

    Installer(root, data, backend, say=lambda _line: None).run("install", manifest)

    assert recovery_calls == ["recovered"]
    stopped = next(call[1] for call in backend.calls if call[0] == "stop")
    assert stopped.release == 9
    assert current(root).release == 10


def test_non_stop_runtime_error_still_runs_update_recovery(tmp_path):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path)
    backend.backup = lambda *_args: {"root": str(root / "backups" / "kept")}
    backend.restore = lambda *_args: backend.calls.append(("restore",))

    def fail_upgrade(*_args):
        raise RuntimeOperationError("Runtime upgrade failed", details={"kind": "migration"})

    backend.upgrade = fail_upgrade
    with pytest.raises(RuntimeOperationError, match="Runtime upgrade failed") as raised:
        Installer(root, data, backend, say=lambda _line: None).run("update", {})

    assert not is_lifecycle_refusal(raised.value)
    assert current(root) == old
    assert [call[0] for call in backend.calls].count("stop") == 2
    assert any(call[0] == "restore" for call in backend.calls)
    assert any(call[0] == "register" for call in backend.calls)


def test_fresh_install_runtime_refusal_does_not_retry_stop(tmp_path):
    root, data = tmp_path / "program", tmp_path / "data"
    backend = LifecycleBackend(root, data)
    failure = RuntimeOperationError("Runtime refused", details={"safe": False})

    def create(*_args):
        backend.calls.append(("create",))
        raise failure

    backend.create = create
    with pytest.raises(RuntimeOperationError) as raised:
        Installer(root, data, backend, say=lambda _line: None).run("install", {})

    assert raised.value is failure
    assert [call[0] for call in backend.calls] == ["stage", "create"]


def test_repair_stop_refusal_does_not_persist_identity(tmp_path):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path)
    persisted = []
    backend.persist_identity = lambda version: persisted.append(version)
    backend.workspace = lambda *_args: {"realm_id": "kept-realm", "realm_root": str(data / "realm")}
    backend.verify = lambda *_args, **_kwargs: (_ for _ in ()).throw(VerificationError("restart required"))
    backend.restart_required = lambda: True

    def refuse(*_args):
        raise RuntimeOperationError("Astrid is busy (1 tasks running). Try again when they finish.",
                                    details={"safe": False, "task_count": 1})

    backend.stop = refuse
    with pytest.raises(RuntimeOperationError, match="Astrid is busy"):
        Installer(root, data, backend, say=lambda _line: None).run("install", {})

    assert persisted == []


def test_candidate_stop_refusal_retries_fenced_stop_before_any_restore(tmp_path):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path)
    backend.backup = lambda *_args: {"root": str(root / "backups" / "kept")}
    backend.upgrade = lambda *_args: None
    stops = []
    original_stop = backend.stop

    def stop(version, stopped_data):
        stops.append(version)
        if version != old:
            raise RuntimeOperationError("Astrid is busy during candidate teardown",
                                        details={"safe": False, "task_count": 1})
        original_stop(version, stopped_data)

    backend.stop = stop
    backend.register = lambda *_args: pytest.fail("refusal must skip activation")
    backend.start = lambda *_args: pytest.fail("refusal must skip relaunch")
    backend.restore = lambda *_args: pytest.fail("refusal must skip restore")

    with pytest.raises(RuntimeOperationError, match="busy during candidate teardown"):
        Installer(root, data, backend, say=lambda _line: None).run("update", {})

    assert len(stops) == 3
    assert stops[0] == old and stops[1] != old and stops[2] == stops[1]
    transaction = read_json(root / "update-transaction.json")
    assert transaction["step"] == "backed_up"
    assert "outcome" not in transaction
    assert current(root) == old


def test_recovery_stop_refusal_is_rethrown_without_activation(tmp_path):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path)
    stops = []

    def stop(version, _data):
        stops.append(version)
        if len(stops) == 2:
            raise RuntimeOperationError("Astrid is busy during recovery",
                                        details={"safe": False, "task_count": 2})

    backend.stop = stop
    backend.backup = lambda *_args: (_ for _ in ()).throw(RuntimeError("backup failed"))
    backend.register = lambda *_args: pytest.fail("refusal must skip activation")
    backend.start = lambda *_args: pytest.fail("refusal must skip relaunch")

    with pytest.raises(RuntimeOperationError, match="busy during recovery") as raised:
        Installer(root, data, backend, say=lambda _line: None).run("update", {})

    assert raised.value.details == {"safe": False, "task_count": 2}
    assert stops == [old, old]
    assert current(root) == old


def test_migration_stopping_recovery_fences_the_still_selected_old_version(tmp_path):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path)
    candidate = backend.stage({}, root, data)
    realm = backend.workspace(old, data)
    write_json(root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "d" * 32,
        "transaction_kind": "migration", "step": "stopping", "data_root": str(data),
        "old": old.document(), "candidate": candidate.document(), "realm": realm,
        "backup": None,
    })

    Installer(root, data, backend, say=lambda _line: None)._recover_update()

    stopped = [call[1] for call in backend.calls if call[0] == "stop"]
    assert stopped == [old]
    assert current(root) == old


def test_skills_sync_failure_is_logged_as_one_line_and_install_succeeds(tmp_path, capsys):
    root, data = tmp_path / "program", tmp_path / "data"
    catalog = data / "runtime" / "catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({"selected_realm_id": "kept-realm"}), encoding="utf-8")
    (data / "runtime" / "realms" / "kept-realm").mkdir(parents=True)
    backend = LifecycleBackend(root, data)
    backend.sync_failure = True

    Installer(root, data, backend).run("install", {})

    assert [call[0] for call in backend.calls][-3:] == ["sync_skills", "finish", "open"]
    assert capsys.readouterr().out.splitlines().count(
        "Couldn't add Astrid's skill to your agents; see the log."
    ) == 1


def test_backend_skill_sync_logs_output_and_failure_result(tmp_path, monkeypatch):
    from astrid.installer.backend import Backend

    log = tmp_path / "install.log"
    messages = []
    backend = Backend(tmp_path / "program", log, Path(sys.executable), say=messages.append)
    calls = []

    def fail(argv, *, extra_env, timeout):
        calls.append((argv, extra_env, timeout))
        log.write_text("sync diagnostic output\n", encoding="utf-8")
        raise subprocess.CalledProcessError(1, argv, output="sync diagnostic output")

    monkeypatch.setattr(backend, "run", fail)
    version = Version(tmp_path / "version", Path(sys.executable), tmp_path / "profile.json", {})
    backend.sync_skills(version)

    assert calls[0][0] == [Path(sys.executable), "-X", "utf8", "-I", "-m", "astrid.skills", "sync"]
    assert calls[0][2] == 60
    assert "sync diagnostic output" in log.read_text(encoding="utf-8")
    assert "Astrid skills sync failed (CalledProcessError)." in log.read_text(encoding="utf-8")
    assert messages == ["Couldn't add Astrid's skill to your agents; see the log."]


@pytest.mark.parametrize("failure", [RuntimeError("Runtime realm creation failed"), KeyboardInterrupt()])
@pytest.mark.parametrize("cleanup", [None, RuntimeError("Task Scheduler unavailable"), KeyboardInterrupt()])
def test_fresh_install_cleanup_preserves_primary_failure(tmp_path, monkeypatch, failure, cleanup):
    root, data = tmp_path / "program", tmp_path / "data"
    backend = LifecycleBackend(root, data)

    def create(*_args):
        raise failure

    def stop(version, stopped_data):
        backend.calls.append(("stop", version, stopped_data))
        if cleanup is not None:
            raise cleanup

    monkeypatch.setattr(backend, "create", create)
    monkeypatch.setattr(backend, "stop", stop)
    with pytest.raises(type(failure)) as raised:
        Installer(root, data, backend, say=lambda _line: None).run("install", {})

    assert raised.value is failure
    assert backend.calls[-1] == ("stop", backend.candidate, data)
    assert not (root / "current").exists()
    if cleanup is not None:
        assert repr(cleanup) in raised.value.__notes__[-1]


def test_reinstall_ignores_current_pointer_to_removed_interpreter(tmp_path):
    root, data = tmp_path / "program", tmp_path / "data"
    stale = root / "versions" / "removed"
    stale.mkdir(parents=True)
    old = Version(stale, stale / "py/bin/python", stale / "profile.json", {})
    write_json(stale / "version.json", old.document())
    switch(root, old)
    catalog = data / "runtime" / "catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text('{"selected_realm_id":"kept-realm"}', encoding="utf-8")
    backend = LifecycleBackend(root, data)

    Installer(root, data, backend, say=lambda _line: None).run("install", {})

    assert [call[0] for call in backend.calls] == [
        "reinstallable", "stage", "attach", "register", "start", "verify", "sync_skills", "finish", "open"
    ]
    assert backend.calls[5][3] == "kept-realm"


def test_unowned_existing_workspace_still_refuses_first_install(tmp_path):
    root, data = tmp_path / "program", tmp_path / "data"
    catalog = data / "runtime" / "catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text('{"selected_realm_id":"kept-realm"}', encoding="utf-8")
    backend = LifecycleBackend(root, data, reinstallable=False)

    with pytest.raises(ValueError, match="existing workspace"):
        Installer(root, data, backend).run("install", {})
    assert [call[0] for call in backend.calls] == ["reinstallable"]
    assert not root.exists()


def test_doctor_uses_installed_version_and_prints_bundle_path(tmp_path):
    root, data = tmp_path / "program", tmp_path / "data"
    folder = root / "versions" / "installed"
    folder.mkdir(parents=True)
    old = Version(folder, folder / "py/bin/python", folder / "profile.json", {})
    old.python.parent.mkdir(parents=True)
    old.python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    write_json(folder / "version.json", old.document())
    switch(root, old)
    bundle = tmp_path / "doctor.zip"
    backend = LifecycleBackend(root, data)
    lines = []

    Installer(root, data, backend, say=lines.append).run("doctor", {}, bundle=bundle)

    assert backend.calls == [("doctor", old, data, bundle)]
    assert lines == [f"Astrid doctor bundle: {bundle}", HELP_MESSAGE]


def test_successful_update_prints_retained_backup_location(tmp_path):
    root, data = tmp_path / "program", tmp_path / "workspace"
    folder = root / "versions" / "installed"
    old = Version(folder, folder / "py/bin/python", folder / "profile.json", {})
    old.python.parent.mkdir(parents=True)
    old.python.write_text("python")
    write_json(folder / "version.json", old.document())
    switch(root, old)
    backup = root / "backups" / "kept-workspace"

    class UpdatingBackend(LifecycleBackend):
        def workspace(self, *_args):
            return {"realm_id": "kept-realm", "realm_root": str(data / "realm")}

        def backup(self, *_args):
            backup.mkdir(parents=True)
            return {"root": str(backup), "kind": "runtime"}

        def upgrade(self, *_args):
            pass

    lines = []
    backend = UpdatingBackend(root, data)

    Installer(root, data, backend, say=lines.append).run("update", {})

    assert backup.is_dir()
    assert f"A backup of your workspace is kept at {backup}." in lines
    assert lines.index(f"A backup of your workspace is kept at {backup}.") < lines.index("Open https://astrid.haus")
    assert "Installing Astrid." in lines


def test_storage_root_uses_install_selection_before_persisted_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("ASTRID_RUNTIME_DATA_ROOT", raising=False)
    monkeypatch.setattr(storage_root, "_read_persisted_support_root", lambda: tmp_path / "persisted")
    monkeypatch.setattr(storage_root, "_read_install_config_root", lambda: tmp_path / "config")
    selection = install_selection(tmp_path / "home", "linux")
    selected_data = tmp_path / "selected-install-data"
    write_json(selection, {"owner": LABEL, "data_root": str(selected_data), "program_root": str(tmp_path / "program")})
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr("astrid.installer.service.install_selection", lambda *_args: selection)

    assert storage_root.resolve_runtime_data_root() == selected_data


def test_explicit_runtime_data_root_still_overrides_install_selection(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(explicit))
    monkeypatch.setattr(storage_root, "_read_installed_support_root", lambda: tmp_path / "installed")

    assert storage_root.resolve_runtime_data_root() == explicit


def test_cli_exposes_doctor_as_an_exclusive_mode_with_optional_bundle_path():
    from astrid.installer.cli import parser

    default = parser().parse_args(["--doctor"])
    chosen = parser().parse_args(["--doctor", "/tmp/astrid-support.zip"])

    assert default.doctor == "__default__"
    assert chosen.doctor == "/tmp/astrid-support.zip"


@pytest.mark.parametrize("operation", ["install", "repair", "update", "uninstall"])
def test_cli_rejects_positional_operations(operation):
    from astrid.installer.cli import parser

    with pytest.raises(SystemExit) as failure:
        parser().parse_args([operation])

    assert failure.value.code == 2


@pytest.mark.parametrize("operation", ["update", "uninstall", "doctor"])
def test_cli_accepts_operation_flags(operation):
    from astrid.installer.cli import parser

    args = parser().parse_args(["--" + operation])

    assert getattr(args, operation)
    assert not hasattr(args, "operation")


def test_cli_explains_backup_space_failure_with_one_sentence(tmp_path, monkeypatch, capsys):
    from runtime_protocol import platform

    from astrid.installer import cli
    from astrid.installer.core import BackupSpaceError

    message = "There is not enough free disk space to back up your workspace."
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")

    def no_space(*_args, **_kwargs):
        raise BackupSpaceError(message)

    monkeypatch.setattr(cli, "Backend", no_space)

    assert cli.main(["--root", str(tmp_path / "program")]) == 1
    assert capsys.readouterr().out.splitlines() == [FAILURE_MESSAGE, message, f"Details: {tmp_path / 'logs/install.log'}"]
    assert message in (tmp_path / "logs/install.log").read_text()


def test_cli_shows_safe_verification_context_but_not_raw_diagnostics(tmp_path, monkeypatch, capsys):
    from runtime_protocol import platform

    from astrid.installer import cli
    from astrid.installer.core import VerificationError

    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    safe = f"pack-host state=restarting reason=worker exited during startup last_exit=7; log={tmp_path / 'service-pack-host.log'}"

    def verification_failed(*_args, **_kwargs):
        raise VerificationError(
            "raw private-token-should-not-appear",
            safe_message=safe,
        )

    monkeypatch.setattr(cli, "Backend", verification_failed)

    assert cli.main(["--root", str(tmp_path / "program")]) == 1
    output = capsys.readouterr().out
    assert safe in output
    assert "private-token-should-not-appear" not in output


@pytest.mark.parametrize(("sandbox_flag", "expected_control"), [(None, "strict"), ("--sandbox", "sandbox")])
def test_cli_loads_manifest_when_current_pointer_targets_missing_interpreter(
    monkeypatch, tmp_path, sandbox_flag, expected_control
):
    from runtime_protocol import platform

    from astrid.installer import cli

    home, root, data = tmp_path / "home", tmp_path / "program", tmp_path / "data"
    home.mkdir(exist_ok=True)
    root.mkdir()
    data.mkdir()
    (root / "install.json").write_text(
        json.dumps({"owner": LABEL, "data_root": str(data)}), encoding="utf-8"
    )
    stale = root / "versions" / "removed"
    stale.mkdir(parents=True)
    old = Version(stale, stale / "py/bin/python", stale / "profile.json", {})
    write_json(stale / "version.json", old.document())
    switch(root, old)
    catalog = data / "runtime" / "catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text('{"selected_realm_id":"kept-realm"}', encoding="utf-8")
    manifest_path, uv = tmp_path / "manifest.json", tmp_path / "uv"
    manifest_path.write_text("{}", encoding="utf-8")
    uv.write_text("", encoding="utf-8")
    record = {"owner": LABEL, "program_root": str(root), "data_root": str(data)}
    backend = LifecycleBackend(root, data)
    process_controls = []

    class CliBackend(LifecycleBackend):
        def __new__(cls, *_args, **kwargs):
            process_controls.append(kwargs["process_control"])
            backend.service = SimpleNamespace(recorded=lambda: record)
            return backend

    monkeypatch.setattr(platform, "private_directory", lambda path: Path(path).mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(platform, "lock_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(platform, "unlock_file", lambda *_args: None)
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(platform, "data_root", lambda: data)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setattr(cli, "Backend", CliBackend)
    monkeypatch.setattr(cli, "validate_manifest", lambda _value: {"pinned": True})
    monkeypatch.setattr(cli, "download", lambda *_args: pytest.fail("manifest should be read from disk"))

    arguments = ["--root", str(root), "--data-root", str(data),
                 "--manifest", str(manifest_path), "--uv", str(uv)]
    if sandbox_flag:
        arguments.append(sandbox_flag)
    result = cli.main(arguments)

    assert result == 0
    assert backend.staged_manifest == {"pinned": True}
    assert any(call[0] == "attach" for call in backend.calls)
    assert process_controls == [expected_control, expected_control]


def test_repeat_uninstall_removes_bootstrap_recreated_program_root(tmp_path, monkeypatch):
    from astrid.installer.backend import Backend

    home, root, data = tmp_path / "home", tmp_path / "program", tmp_path / "data"
    home.mkdir(exist_ok=True)
    data.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv")
    write_json(backend.service.selection, {
        "owner": LABEL, "program_root": str(root), "data_root": str(data), "uninstalled": True
    })

    for _ in range(2):
        (root / "bootstrap").mkdir(parents=True, exist_ok=True)
        (root / "bootstrap" / "uv").write_text("bootstrap residue", encoding="utf-8")
        assert backend.remove(root, data, delete_data=False) == []
        assert not root.exists()


def _installer_runtime(tmp_path, monkeypatch, *, foreground=None):
    from astrid.installer.backend import Backend
    bootstrap = importlib.import_module("astrid_runtime.bootstrap")
    runtime_boundary = importlib.import_module("astrid_runtime.runtime_boundary")
    from astrid_runtime.paths import RuntimePaths
    from runtime_protocol.store import RealmStore

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    root, data = tmp_path / "program", tmp_path / "data"
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv")
    backend.service.systemd = lambda: False
    paths = RuntimePaths.current(data_root=data)
    paths.ensure_support_dirs()
    realm_root = paths.realms_dir / "realm-1"
    RealmStore.initialize(realm_root, realm_id="realm-1").close()
    import astrid_runtime
    from astrid_runtime.bootstrap import SourceProfile
    write_json(paths.source_profiles_dir / "astrid.json", SourceProfile(
        profile="astrid",
        runtime_checkout=str(Path(astrid_runtime.__file__).resolve().parents[1]),
        source_checkout=str(Path(__file__).resolve().parents[1]),
    ).as_dict())
    paths.catalog_path.write_text(json.dumps({
        "version": 1,
        "selected_realm_id": "realm-1",
        "realms": [{"realm_id": "realm-1", "display_name": "Astrid Workspace",
                    "data_root": str(realm_root)}],
        "source_profiles": {},
    }), encoding="utf-8")
    record = {"owner": LABEL, "program_root": str(root), "data_root": str(data)}
    if foreground is not None:
        record["foreground"] = foreground
    backend.service.recorded = lambda: record.copy()
    backend.service.login_service_exists = lambda: False
    manager_calls = []
    backend.service.disable = lambda: manager_calls.append("disable")
    backend.service.stop = lambda: manager_calls.append("stop")

    class Boundary:
        live = True
        identity_error = None
        stop_calls = 0

        def prepare_restart(self, **_kwargs):
            pass

        def is_pid_alive(self, _pid):
            return self.live

        def process_birth_identity(self, _pid):
            return "birth-1" if self.live else None

        def validate_owner(self, **_kwargs):
            if self.identity_error is not None:
                raise self.identity_error
            return _kwargs.get("process_birth_id") == self.process_birth_identity(_kwargs.get("pid"))

        def endpoint_metadata(self, **_kwargs):
            return {"runtime_instance_id": "test-instance", "realm_id": "realm-1"}

        def stop_owner(self, **_kwargs):
            self.stop_calls += 1
            return {"status": "stopped"}

    boundary = Boundary()
    monkeypatch.setattr(runtime_boundary, "LocalRuntimeBoundary", lambda: boundary)
    monkeypatch.setattr(bootstrap.BootstrapConfig, "resolve_source_profile", lambda *_args: object())
    return backend, data, paths, realm_root, boundary, manager_calls


def _publish_owner(paths, *, pid=777, birth="birth-1"):
    identity = {
        "pid": pid, "process_birth_id": birth, "runtime_instance_id": "test-instance",
        "endpoint": "http://127.0.0.1:7777", "active_realm": "realm-1",
        "realm_root": str(paths.realms_dir / "realm-1"),
    }
    write_json(paths.discovery_path, identity)
    write_json(paths.instance_lock_path, {**identity, "realm_id": "realm-1"})
    return identity


def test_backend_stop_uses_runtime_fence_before_service_manager(tmp_path, monkeypatch):
    backend, data, paths, _realm, boundary, manager_calls = _installer_runtime(tmp_path, monkeypatch)
    _publish_owner(paths)
    backend.stop(None, data)

    assert boundary.stop_calls == 1
    assert manager_calls == ["disable", "stop"]
    assert not paths.discovery_path.exists()
    assert not paths.instance_lock_path.exists()


@pytest.mark.parametrize("transaction_kind", ["migration", "app"])
def test_backend_stop_authority_survives_new_controller_and_real_c1_replay(tmp_path, monkeypatch, transaction_kind):
    """The historical authority premise is removed; C1 must still admit a fresh retry."""
    from astrid.installer.backend import Backend

    backend, data, paths, realm, boundary, manager_calls = _installer_runtime(tmp_path, monkeypatch)
    _publish_owner(paths)
    backend.stop(None, data)
    assert boundary.stop_calls == 1
    assert manager_calls == ["disable", "stop"]

    # A newly constructed Backend has no receipt. With no Runtime publication,
    # C1 still admits the selected readable realm and manager teardown retry.
    new_backend = Backend(backend.root, backend.log, backend.uv)
    new_backend.service.systemd = lambda: False
    new_backend.service.recorded = lambda: {"owner": LABEL, "program_root": str(backend.root),
                                             "data_root": str(data)}
    new_backend.service.login_service_exists = lambda: False
    new_backend.service.disable = lambda: manager_calls.append("disable-recovery")
    new_backend.service.stop = lambda: manager_calls.append("stop-recovery")
    fresh_boundary = type(boundary)()
    fresh_boundary.live = False
    from astrid_runtime import runtime_boundary
    monkeypatch.setattr(runtime_boundary, "LocalRuntimeBoundary", lambda: fresh_boundary)
    new_backend.stop(None, data)

    assert fresh_boundary.stop_calls == 0
    assert manager_calls[-2:] == ["disable-recovery", "stop-recovery"]
    assert not paths.discovery_path.exists()
    assert not paths.instance_lock_path.exists()


def test_real_runtime_migration_bootstrap_follows_backend_c1_stop(tmp_path, monkeypatch):
    from astrid_runtime import operator_upgrade, runtime_boundary
    from astrid_runtime.bootstrap import BootstrapConfig, SourceProfile
    from astrid_runtime.identity import requested_startup_identity
    from runtime_protocol.store import SCHEMA_VERSION
    from runtime_protocol.upgrade import inspect_canonical_schema

    # Match a staged install's identity inputs. Reading the manager's working
    # checkout while other tests edit files is neither an installed identity
    # nor a stable source capture.
    monkeypatch.setenv("ASTRID_INSTALL_RELEASE", "18")
    monkeypatch.setenv("ASTRID_INSTALL_MANAGED_BY", "")
    backend, data, paths, realm, boundary, manager_calls = _installer_runtime(tmp_path, monkeypatch)
    source = SourceProfile(
        profile="astrid", runtime_checkout=str(Path(runtime_boundary.__file__).resolve().parents[1]),
        source_checkout=str(Path(__file__).resolve().parents[1]), capability_digest="test-caps",
    )
    monkeypatch.setattr(BootstrapConfig, "resolve_source_profile", lambda _self, _paths: source)
    (paths.runtime_support / "credentials").mkdir(parents=True, exist_ok=True)
    (paths.runtime_support / "credentials" / "owner.token").write_text("test-owner", encoding="utf-8")
    with sqlite3.connect(realm / "realm.sqlite3") as connection:
        connection.execute("ALTER TABLE timelines DROP COLUMN name")
        connection.execute("DROP TABLE user_app_state")
        connection.execute("UPDATE runtime_schema SET version=29 WHERE id=1")

    _publish_owner(paths)
    backend.stop(None, data)

    # Exercise the actual Runtime migration and bootstrap implementation. Only
    # the daemon boundary is modeled; `_bootstrap_locked` remains real.
    boundary.live = False
    boundary.current_pid = 777
    boundary.current_instance = "test-instance"
    boundary.current_birth = "birth-1"

    def start(**_kwargs):
        boundary.live = True
        boundary.current_pid = 778
        boundary.current_instance = "migrated-instance"
        boundary.current_birth = "birth-2"
        return {
            "endpoint": "http://127.0.0.1:7777", "pid": 778,
            "runtime_instance_id": "migrated-instance", "process_birth_id": "birth-2",
            "protocol_version": source.protocol_version, "schema_version": source.schema_version,
            "capability_digest": source.capability_digest,
        }

    boundary.start = start
    boundary.health = lambda **_kwargs: boundary.live
    boundary.is_pid_alive = lambda pid: boundary.live and pid == boundary.current_pid
    boundary.process_birth_identity = lambda pid: boundary.current_birth if boundary.live and pid == boundary.current_pid else None
    boundary.validate_owner = lambda **kwargs: (
        boundary.live and kwargs.get("pid") == boundary.current_pid
        and kwargs.get("process_birth_id") == boundary.current_birth
    )
    boundary.endpoint_metadata = lambda **_kwargs: {
        "runtime_instance_id": boundary.current_instance, "realm_id": "realm-1",
    }
    boundary.owner_startup_identity = lambda **_kwargs: {
        "runtime_instance_id": boundary.current_instance,
        "identity": requested_startup_identity(source),
    }
    boundary.connect = lambda **_kwargs: SimpleNamespace(doctor=lambda: {"ok": True})
    result = operator_upgrade.upgrade_workspace(paths, boundary, BootstrapConfig(source_profile=source))

    assert result["ok"] is True
    assert result["schema_before"] == "v29"
    assert inspect_canonical_schema(realm, expected_realm_id="realm-1")["version"] == SCHEMA_VERSION
    discovery = read_json(paths.discovery_path)
    assert discovery["runtime_instance_id"] == "migrated-instance"
    assert read_json(paths.instance_lock_path)["process_birth_id"] == "birth-2"
    assert manager_calls == ["disable", "stop"]


@pytest.mark.integration
@pytest.mark.linux_native
def test_real_backend_upgrade_binding_survives_fresh_controller_recovery(tmp_path, monkeypatch):
    """Run real migration and rollback activation using selected version identity."""
    import importlib
    import socket
    import time

    from astrid_runtime.bootstrap import SourceProfile
    from astrid_runtime.paths import RuntimePaths

    from astrid.installer import core
    from astrid.installer.backend import Backend
    from astrid.installer.core import Version, write_json
    runtime_root = Path(importlib.import_module("astrid_runtime").__file__).resolve().parents[1]
    agent_root = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    for variable, folder in {
        "HOME": home, "XDG_STATE_HOME": tmp_path / "state",
        "XDG_CONFIG_HOME": tmp_path / "config", "XDG_DATA_HOME": tmp_path / "local-data",
    }.items():
        monkeypatch.setenv(variable, str(folder))
        folder.mkdir(parents=True, exist_ok=True)

    root, data = tmp_path / "program", tmp_path / "data"
    root.mkdir()
    (root / "versions").mkdir()
    source = SourceProfile(profile="astrid", runtime_checkout=str(runtime_root),
                           source_checkout=str(agent_root))
    def installed_version(name, release):
        folder = root / "versions" / name
        folder.mkdir()
        environment = folder / "runtime-env"
        subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(environment)], check=True)
        python = environment / "bin" / "python"
        site = python.parent.parent / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
        (site / "astrid-f2-sources.pth").write_text(
            "import sys; sys.path[:0]="
            f"[{str(runtime_root)!r},{str(runtime_root / 'packages/python')!r},{str(agent_root)!r}]\n",
            encoding="utf-8",
        )
        profile = folder / "profile.json"
        profile.write_text(json.dumps(source.as_dict()), encoding="utf-8")
        return Version(folder, python, profile,
                       {**common_env, "ASTRID_INSTALL_RELEASE": str(release)}, release=release)

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    door_port = probe.getsockname()[1]
    probe.close()
    common_env = {"ASTRID_RUNTIME_DATA_ROOT": str(data), "ASTRID_INSTALL_MANAGED_BY": "",
                 "ASTRID_RUNTIME_DOOR_PORT": str(door_port)}
    old = installed_version("old", 17)
    candidate = installed_version("candidate", 18)
    write_json(old.root / "version.json", old.document())
    write_json(candidate.root / "version.json", candidate.document())
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv")
    backend.service.systemd = lambda: False
    backend.runtime(candidate, data, "workspace", "create", "--realm-root", str(data / "realm"),
                    "--realm-id", "realm-bound", profile=True, timeout=90)
    with sqlite3.connect(data / "realm" / "realm.sqlite3") as connection:
        connection.execute("ALTER TABLE timelines DROP COLUMN name")
        connection.execute("DROP TABLE user_app_state")
        connection.execute("UPDATE runtime_schema SET version=29 WHERE id=1")

    transaction = {
        "format": "astrid-install-update/v1", "operation_id": "8" * 32,
        "transaction_kind": "migration", "step": "backed_up", "data_root": str(data),
        "old": old.document(), "candidate": candidate.document(),
        "realm": {"realm_id": "realm-bound", "realm_root": str(data / "realm")},
    }
    journal = root / "update-transaction.json"
    write_json(journal, transaction)
    def save(step, **details):
        transaction.update(step=step, **details)
        write_json(journal, transaction)

    try:
        backend.upgrade(candidate, data)
        paths = RuntimePaths.current(data_root=data)
        first = read_json(paths.discovery_path)
        first_lock = read_json(paths.instance_lock_path)
        from astrid_runtime.identity import validate_live_owner_identity
        from astrid_runtime.runtime_boundary import LocalRuntimeBoundary

        boundary = LocalRuntimeBoundary()
        first_health = boundary.owner_startup_identity(
            endpoint=first["endpoint"], instance_id=first["runtime_instance_id"],
        )
        assert validate_live_owner_identity(first_health, instance_id=first["runtime_instance_id"]) == {
            "release": 18, "managed_by": None,
        }
        assert first_lock["runtime_instance_id"] == first["runtime_instance_id"]
        assert first_lock["realm_id"] == "realm-bound"

        # Stop the temporary migration daemon through C1, then activate only
        # the exact candidate selected by the journal phase and pointer.
        backend.stop(candidate, data)
        core.switch(root, candidate)
        save("switched")
        backend.register(candidate, data)
        backend.start(candidate, data)

        def assert_selected_live_owner(expected):
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                discovery = read_json(paths.discovery_path)
                lock = read_json(paths.instance_lock_path)
                if (discovery.get("active_realm") == "realm-bound"
                        and discovery.get("runtime_instance_id") == lock.get("runtime_instance_id")
                        and lock.get("realm_id") == "realm-bound"):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError(f"{expected.release}-version Runtime did not publish the selected realm")
            health = LocalRuntimeBoundary().owner_startup_identity(
                endpoint=discovery["endpoint"], instance_id=discovery["runtime_instance_id"],
            )
            assert validate_live_owner_identity(health, instance_id=discovery["runtime_instance_id"]) == {
                "release": expected.release, "managed_by": None,
            }
            assert lock["runtime_instance_id"] == discovery["runtime_instance_id"]
            return discovery

        assert_selected_live_owner(candidate)
        second = read_json(paths.discovery_path)
        second_lock = read_json(paths.instance_lock_path)
        assert second["runtime_instance_id"] != first["runtime_instance_id"]
        assert second_lock["runtime_instance_id"] == second["runtime_instance_id"]
        assert second["active_realm"] == second_lock["realm_id"] == "realm-bound"
        foreground = backend.service.recorded()["foreground"]
        assert foreground["pid"] == second["pid"]

        # Model a crash after the switched candidate became ready but before
        # the transaction reached a terminal phase.

        fresh = Backend(root, tmp_path / "install.log", tmp_path / "uv")
        fresh.service.systemd = lambda: False

        # Recover through the real C1 admission and old-version register/start route.
        # The Runtime and C1 boundaries remain real; only pack-host readiness
        # is narrowed because this fixture has no production pack-host build.

        def verify_old(previous, workspace, realm_id, *, timeout=90):
            assert previous == old
            assert workspace == data
            assert realm_id == "realm-bound"
            discovery = assert_selected_live_owner(old)
            record = fresh.service.recorded()
            assert record["foreground"]["pid"] == discovery["pid"]

        fresh.verify = verify_old
        class ControllerDied(BaseException):
            pass

        first_recovery = Installer(root, data, fresh, say=lambda _line: None)
        original_save = first_recovery._save_update

        def crash_before_terminal(tx, step, **details):
            if step == "rolled_back":
                raise ControllerDied("old Runtime published before terminal journal save")
            original_save(tx, step, **details)

        first_recovery._save_update = crash_before_terminal
        with pytest.raises(ControllerDied):
            first_recovery._recover_update()
        pending = read_json(journal)
        assert pending["step"] == "rollback_selected"
        assert current(root) == old
        published_old = read_json(paths.discovery_path)
        assert published_old["runtime_instance_id"]
        assert published_old["active_realm"] == "realm-bound"
        foreground = fresh.service.recorded()["foreground"]
        assert foreground["pid"] == published_old["pid"]

        # A separate Backend admits and stops the published old Runtime using
        # its canonical selected realm identity, then repeats rollback.
        repeated = Backend(root, tmp_path / "install.log", tmp_path / "uv")
        repeated.service.systemd = lambda: False
        repeated.verify = verify_old
        try:
            Installer(root, data, repeated, say=lambda _line: None)._recover_update()
        finally:
            fresh.stop(old, data)
        assert read_json(journal)["step"] == "rolled_back"
        assert current(root) == old
        assert read_json(old.root / "version.json")["identity"] == {
            "release": 17, "managed_by": None,
        }
    finally:
        # The final C1 stop above normally removes the process; this retries
        # only if an assertion failed before fresh-controller recovery.
        try:
            cleanup = locals().get("repeated", locals().get("fresh", backend))
            selected = current(root) or candidate
            cleanup.stop(selected, data)
        except (RuntimeOperationError, OSError, ValueError):
            pass


@pytest.mark.parametrize("restore_gap", ["realm_removed", "catalog_removed", "support_catalog_removed"])
def test_installer_cli_recovers_realm_restore_gap_through_real_c1(tmp_path, monkeypatch, restore_gap):
    from astrid.installer import cli
    from astrid.installer.backend import Backend

    backend, data, paths, realm, boundary, manager_calls = _installer_runtime(tmp_path, monkeypatch)
    _publish_owner(paths)
    backend.stop(None, data)
    journal = backend.root / "update-transaction.json"

    old_root = backend.root / "versions" / "old"
    candidate_root = backend.root / "versions" / "candidate"
    old_root.mkdir(parents=True)
    candidate_root.mkdir()
    old = Version(old_root, Path(sys.executable), old_root / "profile.json", {}, 17)
    candidate = Version(candidate_root, Path(sys.executable), candidate_root / "profile.json", {}, 18)
    write_json(old_root / "version.json", old.document())
    write_json(candidate_root / "version.json", candidate.document())
    switch(backend.root, candidate)

    backup_root = backend.root / "backups" / "recovery-image"
    backup_root.mkdir(parents=True)
    backend._copy(realm, backup_root / "realm")
    backend._copy(data, backup_root / "support", support=True)
    backup = {"root": str(backup_root), "kind": "clone"}
    write_json(backup_root / "backup.json", backup)
    if restore_gap != "support_catalog_removed":
        with sqlite3.connect(realm / "realm.sqlite3") as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS restore_probe(value TEXT)")
            connection.execute("DELETE FROM restore_probe")
            connection.execute("INSERT INTO restore_probe(value) VALUES ('damaged')")

    if restore_gap == "realm_removed":
        parked = realm.with_name(realm.name + ".restore-pending")
        realm.rename(parked)
    elif restore_gap == "catalog_removed":
        realm.rename(realm.with_name(realm.name + ".restore-pending"))
        paths.catalog_path.unlink()
    else:
        paths.catalog_path.unlink()

    # The new process has no C1 receipt and cannot retain the original child.
    fresh_backend = Backend(backend.root, backend.log, backend.uv)
    fresh_backend.service.systemd = lambda: False
    write_json(fresh_backend.service.selection, {
        "owner": LABEL, "program_root": str(backend.root), "data_root": str(data),
        "realm_id": "realm-1", "realm_root": str(realm),
    })
    fresh_backend.service.recorded = lambda: read_json(fresh_backend.service.selection)
    fresh_backend.service.login_service_exists = lambda: False
    fresh_backend.service.validate_stop_target = lambda *_args, **_kwargs: None
    fresh_backend.service.disable = lambda: manager_calls.append("recovery-disable")
    fresh_backend.service.stop = lambda: manager_calls.append("recovery-stop")
    fresh_boundary = type(boundary)()
    fresh_boundary.live = False
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary", lambda: fresh_boundary)
    ready = []
    fresh_backend.register = lambda version, _data: ready.append(("register", version.root.name))
    fresh_backend.start = lambda version, _data: ready.append(("start", version.root.name))
    fresh_backend.verify = lambda version, _data, realm_id: ready.append(("verify", realm_id))
    pending = {
        "format": "astrid-install-update/v1", "operation_id": "a" * 32,
        "transaction_kind": "migration", "step": "recovering", "data_root": str(data),
        "old": old.document(), "candidate": candidate.document(),
        "realm": {"realm_id": "realm-1", "realm_root": str(realm)},
        "backup": backup,
    }
    write_json(journal, pending)
    write_json(backend.root / "install.json", {"owner": LABEL, "data_root": str(data)})
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: fresh_backend)
    result = cli.main([
        "--root", str(backend.root), "--manifest", str(tmp_path / "unavailable-manifest.json"),
        "--uv", str(tmp_path / "unavailable-uv"),
    ])

    assert result == 0
    assert read_json(journal)["step"] == "rolled_back"
    assert current(backend.root).root == old_root
    assert read_json(paths.catalog_path)["selected_realm_id"] == "realm-1"
    with sqlite3.connect(realm / "realm.sqlite3") as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='restore_probe'").fetchone() is None
    assert ready == [("register", "old"), ("start", "old"), ("verify", "realm-1")]
    assert manager_calls[-2:] == ["recovery-disable", "recovery-stop"]
    assert fresh_boundary.stop_calls == 0


def test_backend_stop_preserves_runtime_busy_details_and_owner(tmp_path, monkeypatch):
    from astrid.installer.core import RuntimeOperationError

    backend, data, paths, realm, boundary, manager_calls = _installer_runtime(tmp_path, monkeypatch)
    _publish_owner(paths)
    with sqlite3.connect(realm / "realm.sqlite3") as connection:
        connection.execute(
            "INSERT INTO tasks (id, run_id, capability, status, created_at, updated_at) "
            "VALUES ('task-busy', 'run-busy', 'test-capability', 'running', ?, ?)",
            ("2026-10-27T00:00:00Z", "2026-10-27T00:00:00Z"),
        )

    with pytest.raises(RuntimeOperationError) as raised:
        backend.stop(None, data)

    assert raised.value.details["headline"] == "Astrid is busy (1 tasks running). Try again when they finish."
    assert raised.value.details["blockers"][0]["task_id"] == "task-busy"
    assert is_lifecycle_refusal(raised.value)
    assert boundary.stop_calls == 0
    assert manager_calls == []
    assert paths.discovery_path.is_file()
    assert paths.instance_lock_path.is_file()


@pytest.mark.parametrize("failure", ["changed_birth", "identity_error", "incomplete_discovery", "malformed_discovery"])
def test_backend_stop_fails_closed_without_manager_action(tmp_path, monkeypatch, failure):
    backend, data, paths, _realm, boundary, manager_calls = _installer_runtime(tmp_path, monkeypatch)
    _publish_owner(paths)
    if failure == "changed_birth":
        boundary.process_birth_identity = lambda _pid: "changed-birth"
    elif failure == "identity_error":
        boundary.identity_error = OSError("identity observation unavailable")
    elif failure == "incomplete_discovery":
        write_json(paths.discovery_path, {"pid": 777, "runtime_instance_id": "test-instance"})
    elif failure == "malformed_discovery":
        paths.discovery_path.write_text("{", encoding="utf-8")

    with pytest.raises(RuntimeError):
        backend.stop(None, data)

    assert boundary.stop_calls == 0
    assert manager_calls == []
    assert paths.instance_lock_path.is_file()


def test_backend_stop_cleans_proven_stale_owner_under_runtime_fence(tmp_path, monkeypatch):
    backend, data, paths, _realm, boundary, manager_calls = _installer_runtime(tmp_path, monkeypatch)
    _publish_owner(paths)
    boundary.live = False
    backend.stop(None, data)

    assert boundary.stop_calls == 0
    assert manager_calls == ["disable", "stop"]
    assert not paths.discovery_path.exists()
    assert not paths.instance_lock_path.exists()


def test_cli_rejects_removed_repair_flag():
    from astrid.installer.cli import parser

    with pytest.raises(SystemExit) as failure:
        parser().parse_args(["--repair"])

    assert failure.value.code == 2


@pytest.mark.parametrize("details", [None, {}, [], "unsafe", {"safe": None},
                                     {"safe": 0}, {"safe": "false"}, {"safe": True},
                                     {"task_count": 1}, {"cause_details": {"safe": False}}])
def test_initial_stop_without_explicit_lifecycle_report_rolls_back(tmp_path, details):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path)
    failure = RuntimeOperationError("stop failed", details=details)
    stops = []

    def stop(version, workspace):
        stops.append((version, workspace))
        if len(stops) == 1:
            raise failure

    backend.stop = stop
    with pytest.raises(RuntimeOperationError) as raised:
        Installer(root, data, backend, say=lambda _: None).run("update", {})

    assert raised.value is failure
    assert stops == [(old, data), (old, data)]
    assert current(root) == old
    assert read_json(root / "update-transaction.json")["step"] == "rolled_back"
    assert [call[0] for call in backend.calls] == [
        "stage", "validate_candidate", "register", "start", "verify"]


@pytest.mark.parametrize("exception_type", [ValueError, KeyboardInterrupt])
def test_initial_non_runtime_stop_exception_retries_before_rollback(tmp_path, exception_type):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path)
    failure = exception_type("stop failed")
    # An arbitrary exception carrying lookalike details is not a Runtime report.
    failure.details = {"safe": False}
    stops = []

    def stop(version, workspace):
        stops.append(version)
        if len(stops) == 1:
            raise failure

    backend.stop = stop
    with pytest.raises(exception_type) as raised:
        Installer(root, data, backend, say=lambda _: None).run("update", {})

    assert raised.value is failure
    assert stops == [old, old]
    assert current(root) == old
    assert read_json(root / "update-transaction.json")["step"] == "rolled_back"


def test_uncertain_stop_then_refused_rollback_leaves_journal_for_repeated_recovery(tmp_path):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path)
    refused = RuntimeOperationError("busy on retry", details={"safe": False, "task_count": 1})
    stops = []

    def stop(version, workspace):
        stops.append(version)
        if len(stops) == 1:
            raise RuntimeOperationError("stop result lost")
        raise refused

    backend.stop = stop
    installer = Installer(root, data, backend, say=lambda _: None)
    with pytest.raises(RuntimeOperationError) as raised:
        installer.run("update", {})
    assert raised.value is refused
    journal = root / "update-transaction.json"
    before = journal.read_bytes()
    assert read_json(journal)["step"] == "stopping"
    for _ in range(3):
        with pytest.raises(RuntimeOperationError) as retry:
            Installer(root, data, backend, say=lambda _: None)._recover_update()
        assert retry.value is refused
        assert journal.read_bytes() == before
        assert current(root) == old
    assert [call[0] for call in backend.calls] == ["stage", "validate_candidate"]
    backend.stop = lambda *_: None
    Installer(root, data, backend, say=lambda _: None)._recover_update()
    assert read_json(journal)["step"] == "rolled_back"
    assert current(root) == old
    assert [call[0] for call in backend.calls][-3:] == ["register", "start", "verify"]


@pytest.mark.parametrize("phase", ["backup", "upgrade", "verify"])
def test_later_lifecycle_report_runs_rollback_without_terminal_refusal(tmp_path, phase):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path)
    failure = RuntimeOperationError("later refusal", details={"safe": False})
    calls = []
    original = getattr(backend, phase)

    def fail_once(*args, **kwargs):
        calls.append(phase)
        if len(calls) == 1:
            raise failure
        return original(*args, **kwargs)

    setattr(backend, phase, fail_once)
    backend.restore = lambda *_: backend.calls.append(("restore",))
    with pytest.raises(RuntimeOperationError) as raised:
        Installer(root, data, backend, say=lambda _: None).run("update", {})
    assert raised.value is failure
    journal = read_json(root / "update-transaction.json")
    assert journal["step"] == "rolled_back"
    assert "outcome" not in journal
    assert current(root) == old
    assert [call[0] for call in backend.calls].count("stop") >= 2


def test_real_c1_post_stop_failure_retries_fence_before_old_activation(tmp_path, monkeypatch):
    backend, data, paths, realm, boundary, manager_calls = _installer_runtime(tmp_path, monkeypatch)
    root = backend.root
    folder = root / "versions" / "old"
    folder.mkdir(parents=True)
    old = Version(folder, Path(sys.executable), folder / "profile.json", {})
    write_json(folder / "version.json", old.document())
    switch(root, old)
    candidate = Version(root / "versions" / "new", Path(sys.executable), folder / "profile.json", {})
    record = {"owner": LABEL, "program_root": str(root), "data_root": str(data)}
    write_json(backend.service.selection, record)
    backend.service.validate_stop_target = lambda *_args, **_kwargs: None
    backend.publish_launch_admission = lambda *_: None
    _publish_owner(paths)
    admissions = []
    from contextlib import contextmanager
    from runtime_protocol.lifecycle import interruption_fence
    bootstrap = importlib.import_module("astrid_runtime.bootstrap")

    @contextmanager
    def fence(selected_realm):
        admissions.append(selected_realm)
        with interruption_fence(selected_realm) as report:
            yield report

    monkeypatch.setattr(bootstrap, "interruption_fence", fence)
    failure = RuntimeOperationError("manager failed after stop", details={"safe": False, "task_count": 1})

    def manager_stop():
        manager_calls.append("stop")
        if manager_calls.count("stop") == 1:
            raise failure

    backend.service.stop = manager_stop
    activated = []
    backend.stage = lambda *_: candidate
    backend.validate_candidate = lambda *_: None
    backend.has_workspace = lambda *_: True
    backend.workspace = lambda *_: {"realm_id": "realm-1", "realm_root": str(realm)}
    backend.backup = lambda *_: pytest.fail("failed stop cannot start backup")
    backend.register = lambda previous, _: activated.append(("register", previous))
    backend.start = lambda previous, _: activated.append(("start", previous))
    backend.verify = lambda previous, *_: activated.append(("verify", previous))
    backend.finish = lambda *_: pytest.fail("failed update cannot finish")
    with pytest.raises(RuntimeOperationError) as raised:
        Installer(root, data, backend, say=lambda _: None).run("update", {})

    assert raised.value.details == {"cause_details": failure.details}
    assert len(admissions) == 2
    assert boundary.stop_calls == 2
    assert manager_calls == ["disable", "stop", "disable", "stop"]
    assert activated == [("register", old), ("start", old), ("verify", old)]
    assert read_json(root / "update-transaction.json")["step"] == "rolled_back"
    assert current(root) == old
