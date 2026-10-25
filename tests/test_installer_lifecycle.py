"""Lifecycle coverage for reinstalling a kept workspace and installed doctor."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer.core import Installer, VerificationError, Version, switch, write_json
from astrid.installer.service import LABEL, install_selection
from astrid.installer.support import FAILURE_MESSAGE, HELP_MESSAGE
from astrid.sdk import storage_root


def test_runtime_commands_do_not_inject_rejected_render_startup_overrides(tmp_path, monkeypatch):
    from astrid.core.rendering.remotion_runtime import probe_context
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
    probe_context(environment=environment, python_executable=sys.executable)


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

    assert calls[0][0] == [Path(sys.executable), "-I", "-m", "astrid.skills", "sync"]
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


def test_cli_loads_manifest_when_current_pointer_targets_missing_interpreter(monkeypatch, tmp_path):
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

    class CliBackend(LifecycleBackend):
        def __new__(cls, *_args, **_kwargs):
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

    result = cli.main(["--root", str(root), "--data-root", str(data),
                       "--manifest", str(manifest_path), "--uv", str(uv)])

    assert result == 0
    assert backend.staged_manifest == {"pinned": True}
    assert any(call[0] == "attach" for call in backend.calls)


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


def _runtime_with_discovery(tmp_path, monkeypatch, pid, birth, *, foreground=False):
    from astrid.installer.backend import Backend

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    root, data = tmp_path / "program", tmp_path / "data"
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv")
    backend.service.systemd = lambda: False
    discovery = data / "runtime" / "discovery.json"
    write_json(discovery, {"pid": pid, "process_birth_id": birth,
                           "runtime_instance_id": "test-instance"})
    if foreground:
        write_json(backend.service.selection, {
            "owner": LABEL, "program_root": str(root), "data_root": str(data),
            "foreground": {"pid": pid, "birth_id": birth},
        })
    return backend, data, discovery


def test_repair_skips_down_for_proven_dead_runtime_and_keeps_discovery(tmp_path, monkeypatch):
    from runtime_protocol import platform

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        identity = platform.process_identity(child.pid)
        assert identity and identity["birth_id"]
        child.kill()
        child.wait(timeout=5)
        backend, data, discovery = _runtime_with_discovery(
            tmp_path, monkeypatch, child.pid, identity["birth_id"], foreground=True
        )
        folder = backend.root / "versions" / "installed"
        folder.mkdir(parents=True)
        old = Version(folder, folder / "py/bin/python", folder / "profile.json", {})
        old.python.parent.mkdir(parents=True)
        old.python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        write_json(folder / "version.json", old.document())
        switch(backend.root, old)
        backend.runtime = lambda *_args, **_kwargs: pytest.fail("must not call down for a proven dead owner")
        backend.workspace = lambda *_args: {"realm_id": "kept-realm", "realm_root": str(data / "realm")}
        calls = []
        backend.register = lambda *_args: calls.append("register")
        backend.start = lambda *_args: calls.append("start")
        def verify(*_args, timeout=90):
            if timeout == 3:
                calls.append("verify-unhealthy")
                raise VerificationError("the recorded Runtime is dead")
            calls.append("verify")
        backend.verify = verify
        backend.open = lambda: calls.append("open")

        Installer(backend.root, data, backend, say=lambda _line: None).run("install", {})

        assert calls == ["verify-unhealthy", "register", "start", "verify", "open"]
        assert discovery.is_file(), "Runtime startup must reconcile stale discovery"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_stop_uses_runtime_down_for_matching_live_owner(tmp_path, monkeypatch):
    from runtime_protocol import platform

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        identity = platform.process_identity(child.pid)
        assert identity and identity["birth_id"]
        backend, data, discovery = _runtime_with_discovery(
            tmp_path, monkeypatch, child.pid, identity["birth_id"]
        )
        calls = []

        def runtime(_version, _data, *args, **_kwargs):
            calls.append(args)
            discovery.unlink()
            return {}

        backend.runtime = runtime
        backend.stop(None, data)

        assert calls == [("down",)]
        assert not discovery.exists()
    finally:
        child.terminate()
        child.wait(timeout=5)


@pytest.mark.parametrize("observation", ["changed_birth", "error"])
def test_stop_fails_closed_when_runtime_owner_identity_is_uncertain(tmp_path, monkeypatch, observation):
    from runtime_protocol import platform

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        observed = platform.process_identity(child.pid)
        assert observed and observed["birth_id"]
        backend, data, discovery = _runtime_with_discovery(
            tmp_path, monkeypatch, child.pid, observed["birth_id"]
        )
        if observation == "changed_birth":
            monkeypatch.setattr(platform, "process_identity", lambda pid: {
                "pid": pid, "birth_id": "a-different-process-incarnation"
            })
        else:
            def fail_observation(_pid):
                raise OSError("identity observation unavailable")
            monkeypatch.setattr(platform, "process_identity", fail_observation)
        backend.runtime = lambda *_args, **_kwargs: pytest.fail("must not signal an uncertain owner")
        stopped = []
        backend.service.stop = lambda: stopped.append(True)

        with pytest.raises(RuntimeError, match="identity"):
            backend.stop(None, data)

        assert discovery.is_file()
        assert stopped == []
    finally:
        child.terminate()
        child.wait(timeout=5)


@pytest.mark.parametrize(("pid", "birth", "missing_birth"), [
    (True, "birth-1", False), (0, "birth-1", False), ("7", "birth-1", False),
    (7, "", False), (7, None, True),
])
def test_stop_fails_closed_for_incomplete_discovery_identity(
    tmp_path, monkeypatch, pid, birth, missing_birth
):
    backend, data, discovery = _runtime_with_discovery(tmp_path, monkeypatch, pid, birth)
    if missing_birth:
        value = json.loads(discovery.read_text(encoding="utf-8"))
        del value["process_birth_id"]
        write_json(discovery, value)
    backend.runtime = lambda *_args, **_kwargs: pytest.fail("must not signal an incomplete owner record")
    stopped = []
    backend.service.stop = lambda: stopped.append(True)

    with pytest.raises(RuntimeError, match="identity"):
        backend.stop(None, data)

    assert discovery.is_file()
    assert stopped == []


@pytest.mark.parametrize("contents", ["{}", "[]", "{"])
def test_stop_does_not_treat_malformed_or_empty_discovery_as_removed(tmp_path, monkeypatch, contents):
    backend, data, discovery = _runtime_with_discovery(tmp_path, monkeypatch, 7, "birth-1")
    discovery.write_text(contents, encoding="utf-8")
    backend.runtime = lambda *_args, **_kwargs: pytest.fail("must not signal an unverified owner")
    stopped = []
    backend.service.stop = lambda: stopped.append(True)

    with pytest.raises(RuntimeError, match="identity"):
        backend.stop(None, data)

    assert discovery.is_file()
    assert stopped == []


def test_stop_treats_discovery_removed_during_read_as_stopped(tmp_path, monkeypatch):
    backend, data, discovery = _runtime_with_discovery(tmp_path, monkeypatch, 7, "birth-1")
    original_read_text = Path.read_text
    service_stopped = []

    def remove_before_read(path, *args, **kwargs):
        if path == discovery:
            path.unlink()
            raise FileNotFoundError(path)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", remove_before_read)
    backend.runtime = lambda *_args, **_kwargs: pytest.fail("removed discovery needs no Runtime down")
    backend.service.stop = lambda: service_stopped.append(True)

    backend.stop(None, data)

    assert service_stopped == [True]
    assert not discovery.exists()


def test_cli_rejects_removed_repair_flag():
    from astrid.installer.cli import parser

    with pytest.raises(SystemExit) as failure:
        parser().parse_args(["--repair"])

    assert failure.value.code == 2
