"""Lifecycle coverage for reinstalling a kept workspace and installed doctor."""

import json
import sqlite3
import importlib
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer.core import Installer, RuntimeOperationError, VerificationError, Version, current, read_json, switch, write_json
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
        self.candidate = Version(folder, folder / "py/bin/python", folder / "profile.json", {})
        self.candidate.python.parent.mkdir(parents=True)
        self.candidate.python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        write_json(folder / "version.json", self.candidate.document())
        return self.candidate

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

    def verify(self, version, data, realm_id):
        self.calls.append(("verify", version, data, realm_id))

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
    assert names.index("upgrade") < names.index("verify") < names.index("sync_skills") < names.index("finish")


@pytest.mark.parametrize("category", ["busy", "timeout", "identity"])
def test_runtime_stop_refusal_preserves_structured_details_without_rollback(tmp_path, category):
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

    def refuse(*_):
        backend.calls.append(("stop", old, data))
        raise RuntimeOperationError("Astrid is busy (1 tasks running). Try again when they finish.",
                                    details=details, stop_refused=True)

    backend.stop = refuse
    installer = Installer(root, data, backend, say=lambda _line: None)
    with pytest.raises(RuntimeOperationError) as raised:
        installer.run("update", {})

    assert raised.value.details == details
    assert current(root) == old
    assert [call[0] for call in backend.calls] == ["stop"]
    assert read_json(root / "update-transaction.json")["step"] == "prepared"


def test_explicit_partial_stop_evidence_allows_update_recovery(tmp_path):
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
                                        stop_refused=False, stop_completed=True)

    backend.stop = partial_stop
    with pytest.raises(RuntimeOperationError):
        Installer(root, data, backend, say=lambda _line: None).run("update", {})

    assert [call[0] for call in backend.calls] == ["stop", "stop", "register", "start", "verify"]
    assert current(root) == old


def _installed_lifecycle_backend(tmp_path):
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
    return root, data, old, backend


def test_non_stop_runtime_error_still_runs_update_recovery(tmp_path):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path)
    backend.backup = lambda *_args: {"root": str(root / "backups" / "kept")}
    backend.restore = lambda *_args: backend.calls.append(("restore",))

    def fail_upgrade(*_args):
        raise RuntimeOperationError("Runtime upgrade failed", details={"kind": "migration"})

    backend.upgrade = fail_upgrade
    with pytest.raises(RuntimeOperationError, match="Runtime upgrade failed") as raised:
        Installer(root, data, backend, say=lambda _line: None).run("update", {})

    assert raised.value.stop_refused is False
    assert current(root) == old
    assert [call[0] for call in backend.calls].count("stop") == 2
    assert any(call[0] == "restore" for call in backend.calls)
    assert any(call[0] == "register" for call in backend.calls)


def test_repair_stop_refusal_does_not_persist_identity(tmp_path):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path)
    persisted = []
    backend.persist_identity = lambda version: persisted.append(version)
    backend.workspace = lambda *_args: {"realm_id": "kept-realm", "realm_root": str(data / "realm")}
    backend.verify = lambda *_args, **_kwargs: (_ for _ in ()).throw(VerificationError("restart required"))
    backend.restart_required = lambda: True

    def refuse(*_args):
        raise RuntimeOperationError("Astrid is busy (1 tasks running). Try again when they finish.",
                                    details={"task_count": 1}, stop_refused=True)

    backend.stop = refuse
    with pytest.raises(RuntimeOperationError, match="Astrid is busy"):
        Installer(root, data, backend, say=lambda _line: None).run("install", {})

    assert persisted == []


def test_candidate_stop_refusal_never_retries_or_rolls_back(tmp_path):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path)
    backend.backup = lambda *_args: {"root": str(root / "backups" / "kept")}
    backend.upgrade = lambda *_args: None
    stops = []
    original_stop = backend.stop

    def stop(version, stopped_data):
        stops.append(version)
        if version != old:
            raise RuntimeOperationError("Astrid is busy during candidate teardown",
                                        details={"task_count": 1}, stop_refused=True)
        original_stop(version, stopped_data)

    backend.stop = stop
    backend.register = lambda *_args: pytest.fail("refusal must skip activation")
    backend.start = lambda *_args: pytest.fail("refusal must skip relaunch")
    backend.restore = lambda *_args: pytest.fail("refusal must skip restore")

    with pytest.raises(RuntimeOperationError, match="busy during candidate teardown"):
        Installer(root, data, backend, say=lambda _line: None).run("update", {})

    assert len(stops) == 2
    assert stops[0] == old and stops[1] != old
    assert current(root) == old


def test_recovery_stop_refusal_is_rethrown_without_activation(tmp_path):
    root, data, old, backend = _installed_lifecycle_backend(tmp_path)
    stops = []

    def stop(version, _data):
        stops.append(version)
        if len(stops) == 2:
            raise RuntimeOperationError("Astrid is busy during recovery",
                                        details={"task_count": 2}, stop_refused=True)

    backend.stop = stop
    backend.backup = lambda *_args: (_ for _ in ()).throw(RuntimeError("backup failed"))
    backend.register = lambda *_args: pytest.fail("refusal must skip activation")
    backend.start = lambda *_args: pytest.fail("refusal must skip relaunch")

    with pytest.raises(RuntimeOperationError, match="busy during recovery") as raised:
        Installer(root, data, backend, say=lambda _line: None).run("update", {})

    assert raised.value.details == {"task_count": 2}
    assert stops == [old, old]
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
    assert raised.value.stop_refused is True
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
