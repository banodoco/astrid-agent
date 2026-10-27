"""The login owner reads its settings without depending on a shell profile."""

import json
import sys
import xml.etree.ElementTree as ET
from types import ModuleType
from unittest.mock import Mock

import pytest

from astrid.installer.backend import Backend
from astrid.installer.core import Version, read_json, write_json
from astrid.installer.service import LABEL, Service, command, default_program_root, render


def _selected_version(tmp_path, *, managed_by=None, release=37):
    from astrid.installer.core import switch

    root = tmp_path / "program"
    version_root = root / "versions" / "v1"
    version_root.mkdir(parents=True)
    version = Version(version_root, tmp_path / "python", version_root / "profile.json", {},
                      release, managed_by)
    write_json(version_root / "version.json", version.document())
    switch(root, version)
    return version


def test_installer_runtime_command_sets_utf8_before_isolation(tmp_path, monkeypatch):
    backend = Backend(tmp_path / "program", tmp_path / "log", tmp_path / "uv")
    version = Version(tmp_path / "version", tmp_path / "python", tmp_path / "profile", {})
    run = Mock(return_value='{"ok":true}')
    monkeypatch.setattr(backend, "run", run)

    backend.runtime(version, tmp_path / "data", "status")

    assert run.call_args.args[0][:6] == [
        version.python, "-X", "utf8", "-I", "-m", "astrid_runtime",
    ]


def test_windows_stop_validation_accepts_exact_canonical_task_action(tmp_path, monkeypatch):
    from astrid.installer.core import switch

    root = tmp_path / "program"
    manager = Service(root, tmp_path / "log", Mock(), home=tmp_path, system="win32")
    version = Version(root / "versions" / "candidate", tmp_path / "python.exe",
                      tmp_path / "profile.json", {})
    version.root.mkdir(parents=True)
    write_json(version.root / "version.json", version.document())
    switch(root, version)
    write_json(manager.selection, {
        "owner": LABEL, "program_root": str(root), "data_root": str(tmp_path / "data"),
    })
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    manager.path.write_text(render("win32", version, tmp_path / "data", tmp_path / "log"), encoding="utf-8")
    xml_root = ET.fromstring(manager.path.read_text(encoding="utf-8"))
    namespace = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    actions = [{
        "Execute": xml_root.findtext(".//task:Actions/task:Exec/task:Command", namespaces=namespace),
        "Arguments": xml_root.findtext(".//task:Actions/task:Exec/task:Arguments", namespaces=namespace),
    }]
    monkeypatch.setattr(manager, "powershell", lambda _script: json.dumps(actions))

    manager.validate_stop_target(
        version, tmp_path / "data", owner_pid=None,
    )


@pytest.mark.parametrize("mutation", ["executable", "profile", "argument"])
def test_windows_stop_validation_rejects_mutated_canonical_task_action(tmp_path, monkeypatch, mutation):
    from astrid.installer.core import switch

    root = tmp_path / "program"
    manager = Service(root, tmp_path / "log", Mock(), home=tmp_path, system="win32")
    version = Version(root / "versions" / "candidate", tmp_path / "python.exe",
                      tmp_path / "profile.json", {})
    version.root.mkdir(parents=True)
    write_json(version.root / "version.json", version.document())
    switch(root, version)
    write_json(manager.selection, {
        "owner": LABEL, "program_root": str(root), "data_root": str(tmp_path / "data"),
    })
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    manager.path.write_text(render("win32", version, tmp_path / "data", tmp_path / "log"), encoding="utf-8")
    xml_root = ET.fromstring(manager.path.read_text(encoding="utf-8"))
    namespace = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    action = {
        "Execute": xml_root.findtext(".//task:Actions/task:Exec/task:Command", namespaces=namespace),
        "Arguments": xml_root.findtext(".//task:Actions/task:Exec/task:Arguments", namespaces=namespace),
    }
    if mutation == "executable":
        action["Execute"] = str(tmp_path / "other-python.exe")
    elif mutation == "profile":
        action["Arguments"] = action["Arguments"].replace(str(version.profile), str(tmp_path / "other-profile.json"))
    else:
        action["Arguments"] += " --unexpected"
    monkeypatch.setattr(manager, "powershell", lambda _script: json.dumps([action]))
    before = {
        "task": manager.path.read_bytes(),
        "selection": manager.selection.read_bytes(),
    }

    with pytest.raises(ValueError, match="scheduled task does not match"):
        manager.validate_stop_target(version, tmp_path / "data", owner_pid=None)

    assert manager.path.read_bytes() == before["task"]
    assert manager.selection.read_bytes() == before["selection"]


@pytest.mark.parametrize("release,managed_by", [(0, None), (37, "pinokio")])
def test_service_wrapper_loads_settings_before_runtime(tmp_path, monkeypatch, release, managed_by):
    settings = tmp_path / "service-env.json"
    write_json(settings, {"ASTRID_WARM_IDLE_SECONDS": "17", "ASTRID_ALLOW_UNQUALIFIED_GPU": "1"})
    monkeypatch.setenv("ASTRID_WARM_IDLE_SECONDS", "900")
    monkeypatch.setenv("ASTRID_ALLOW_UNQUALIFIED_GPU", "0")
    version = _selected_version(tmp_path, managed_by=managed_by, release=release)
    observed = []
    module = ModuleType("astrid_runtime.cli")

    def main(argv):
        import os

        observed.append((argv, os.environ["ASTRID_WARM_IDLE_SECONDS"], os.environ["ASTRID_ALLOW_UNQUALIFIED_GPU"],
                         os.environ["ASTRID_INSTALL_RELEASE"], os.environ["ASTRID_INSTALL_MANAGED_BY"]))
        return 0

    module.main = main
    monkeypatch.setitem(sys.modules, "astrid_runtime.cli", module)
    argv = command(version, tmp_path / "data", env_file=settings)
    with pytest.raises(SystemExit) as result:
        exec(argv[5], {})
    assert result.value.code == 0
    assert observed == [(["run", "--profile", "astrid", "--data-root", str(tmp_path / "data"),
                         "--source-manifest", str(version.profile)], "17", "1", str(release), managed_by or "")]


@pytest.mark.parametrize("settings", [{"PATH": "/unmanaged"}, {"ASTRID_WARM_IDLE_SECONDS": 3}, []])
def test_service_wrapper_rejects_unknown_or_nonstring_settings(tmp_path, settings):
    path = tmp_path / "service-env.json"
    path.write_text(json.dumps(settings))
    version = _selected_version(tmp_path)
    with pytest.raises(ValueError, match="Invalid Astrid service-env.json"):
        exec(command(version, tmp_path / "data", env_file=path)[5], {})


def test_registration_preserves_settings_and_records_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    root = tmp_path / "program"
    manager = Service(root, tmp_path / "log", Mock(side_effect=FileNotFoundError()), home=tmp_path, system="linux", say=lambda _: None)
    write_json(manager.env_file, {"ASTRID_WARM_IDLE_SECONDS": "17"})
    before = manager.env_file.read_bytes()
    version = Version(root / "versions/one", tmp_path / "python", tmp_path / "profile", {}, 12)
    write_json(manager.selection, {"owner": LABEL, "other_record_field": "preserved"})
    manager.register(version, tmp_path / "data")
    manager.register(version, tmp_path / "data")
    assert manager.env_file.read_bytes() == before
    assert str(manager.env_file) in manager.path.read_text()
    assert read_json(manager.selection)["program_root"] == str(root)
    assert read_json(manager.selection)["service_settings"] == {"ASTRID_WARM_IDLE_SECONDS": "17"}
    assert read_json(manager.selection)["identity"] == {"release": 12, "managed_by": None}
    assert read_json(manager.selection)["other_record_field"] == "preserved"
    assert default_program_root(tmp_path, "linux") == tmp_path / "xdg-data/astrid"


def test_healthy_noop_repair_migrates_identity_without_service_registration(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    root, data = tmp_path / "program", tmp_path / "data"
    version_root = root / "versions" / "v1"
    version_root.mkdir(parents=True)
    python = version_root / "python"
    python.write_text("runtime", encoding="utf-8")
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    write_json(version_root / "manifest.json", {"release": 19})
    write_json(version_root / "version.json", {
        "root": str(version_root), "python": str(python),
        "profile": str(version_root / "profile.json"), "env": {},
    })
    (root / "current").write_text("v1\n", encoding="utf-8")
    manager = Backend(root, tmp_path / "log", tmp_path / "uv", say=lambda _line: None)
    write_json(manager.service.selection, {
        "owner": LABEL, "program_root": str(root), "data_root": str(data),
        "unrelated": {"preserve": True},
        "preferences": {"schema_version": 1, "autostart": False},
    })
    manager.workspace = lambda *_args: {"realm_id": "realm", "realm_root": str(data / "realm")}
    manager.verify = lambda *_args, **_kwargs: None
    manager.restart_required = lambda: False
    manager.open = lambda: None
    manager.service.register = lambda *_args: pytest.fail("healthy repair must not re-register")

    from astrid.installer.core import Installer
    Installer(root, data, manager, say=lambda _line: None).run("install", {"release": 19})

    assert json.loads((version_root / "version.json").read_text())["identity"] == {
        "release": 19, "managed_by": None,
    }
    selected = read_json(manager.service.selection)
    assert selected["identity"] == {"release": 19, "managed_by": None}
    assert selected["unrelated"] == {"preserve": True}
    assert selected["preferences"] == {"schema_version": 1, "autostart": False}

    # A second healthy repair consumes the stamp and does not repeat migration.
    monkeypatch.setattr("astrid.installer.core.write_json", lambda *_args: pytest.fail("already migrated"))
    Installer(root, data, manager, say=lambda _line: None).run("install", {"release": 19})


@pytest.mark.parametrize("managed_by", [None, "pinokio"])
def test_backend_registration_keeps_selection_fields_and_records_selected_identity(tmp_path, monkeypatch, managed_by):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    root, data = tmp_path / "program", tmp_path / "data"
    version_root = root / "versions" / "v1"
    version_root.mkdir(parents=True)
    version = Version(version_root, tmp_path / "python", tmp_path / "profile", {}, 29, managed_by)
    backend = Backend(root, tmp_path / "log", tmp_path / "uv", managed_by=managed_by)
    write_json(backend.service.selection, {"owner": LABEL, "unrelated": "retained", "uninstalled": True})
    def register(selected, _data):
        # A sign-in manager may start immediately when registered.
        assert read_json(backend.service.selection)["identity"] == selected.document()["identity"]
        assert read_json(version_root / "version.json")["env"] == selected.env

    backend.service.register = register
    backend.workspace = lambda *_args: {"realm_id": "realm", "realm_root": str(data / "realm")}
    backend._prior_chat_settings = lambda *_args: (None, {})

    backend.register(version, data)

    selected = read_json(backend.service.selection)
    assert selected["identity"] == {"release": 29, "managed_by": managed_by}
    assert selected["unrelated"] == "retained"
    assert "uninstalled" not in selected


@pytest.mark.parametrize("settings", [
    {"ASTRID_WARM_IDLE_SECONDS": "18"}, {"ASTRID_ALLOW_UNQUALIFIED_GPU": "1"}, {},
])
def test_changed_or_cleared_service_settings_require_restart(tmp_path, monkeypatch, settings):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    root = tmp_path / "program"
    manager = Service(root, tmp_path / "log", Mock(side_effect=FileNotFoundError()),
                      home=tmp_path, system="linux", say=lambda _: None)
    write_json(manager.env_file, {"ASTRID_WARM_IDLE_SECONDS": "17"})
    version = Version(root / "versions/one", tmp_path / "python", tmp_path / "profile", {})
    manager.register(version, tmp_path / "data")
    assert manager.restart_required() is False

    write_json(manager.env_file, settings)

    assert manager.restart_required() is True
    manager.register(version, tmp_path / "data")
    assert manager.restart_required() is False


@pytest.mark.parametrize("settings,expected", [({}, False), ({"ASTRID_WARM_IDLE_SECONDS": "17"}, True)])
def test_legacy_services_restart_for_nonempty_unrecorded_settings(tmp_path, settings, expected):
    manager = Service(tmp_path / "program", tmp_path / "log", Mock(), home=tmp_path, system="linux")
    write_json(manager.selection, {"owner": LABEL})
    write_json(manager.env_file, settings)

    assert manager.restart_required() is expected


def test_foreground_service_uses_its_own_log(tmp_path, monkeypatch):
    from runtime_protocol import platform

    manager = Service(tmp_path / "program", tmp_path / "install.log", Mock(),
                      home=tmp_path, system="linux")
    version = Version(tmp_path / "program", tmp_path / "python", tmp_path / "profile", {})
    observed = []

    def spawn(_argv, **kwargs):
        observed.append(kwargs["stdout"].name)
        return Mock(pid=42)

    monkeypatch.setattr(platform, "spawn_tree", spawn)
    monkeypatch.setattr(platform, "process_identity", lambda _pid: {"birth_id": "owner"})

    manager.start_foreground(version, tmp_path / "data")

    assert observed == [str(tmp_path / "runtime-service.log")]
    assert not (tmp_path / "install.log").exists()


def test_removed_install_can_delete_kept_data_repeatedly(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    root, data = tmp_path / "removed-program", tmp_path / "kept-data"
    data.mkdir()
    (data / "sentinel").write_text("kept")
    backend = Backend(root, tmp_path / "log", tmp_path / "uv")
    write_json(backend.service.selection, {"owner": LABEL, "program_root": str(root),
                                         "data_root": str(data), "uninstalled": True})
    assert backend.remove(root, data, delete_data=False) == []
    assert (data / "sentinel").read_text() == "kept"
    assert backend.remove(root, data, delete_data=True) == []
    assert not data.exists()
    assert backend.remove(root, data, delete_data=True) == []
    assert not root.exists()


def test_doctor_does_not_report_a_stale_bundle_after_failure(tmp_path, monkeypatch):
    import subprocess

    backend = Backend(tmp_path / "program", tmp_path / "log", tmp_path / "uv")
    version = Version(tmp_path / "version", tmp_path / "python", tmp_path / "profile", {})
    bundle = tmp_path / "doctor.zip"
    bundle.write_bytes(b"previous report")
    monkeypatch.setattr(backend, "run", Mock(side_effect=subprocess.CalledProcessError(1, "doctor")))
    with pytest.raises(subprocess.CalledProcessError):
        backend.doctor(version, tmp_path / "data", bundle)
    assert bundle.read_bytes() == b"previous report"


def test_doctor_returns_a_new_bundle_for_an_unhealthy_runtime(tmp_path, monkeypatch):
    import subprocess

    backend = Backend(tmp_path / "program", tmp_path / "log", tmp_path / "uv")
    version = Version(tmp_path / "version", tmp_path / "python", tmp_path / "profile", {})
    bundle = tmp_path / "doctor.zip"
    calls = []
    reveal = Mock()
    monkeypatch.setattr("astrid.installer.backend.reveal_bundle", reveal)

    def unhealthy(argv, *, extra_env):
        calls.append((argv, extra_env))
        bundle.write_bytes(b"new unhealthy report")
        raise subprocess.CalledProcessError(1, "doctor")

    monkeypatch.setattr(backend, "run", unhealthy)
    assert backend.doctor(version, tmp_path / "data", bundle) == bundle
    assert calls[0][1]["ASTRID_RUNTIME_DATA_ROOT"] == str(tmp_path / "data")
    reveal.assert_called_once_with(bundle)


@pytest.mark.parametrize("step,selected,allowed", [
    (None, "old", True),
    ("staged", "old", False), ("stopping", "old", False),
    ("stopped", "old", False), ("backed_up", "old", False),
    ("migrated", "old", False), ("migrated", "candidate", False),
    ("switched", "candidate", True), ("switched", "old", False),
    ("verified", "candidate", False), ("recovering", "old", False),
    ("rollback_selected", "old", True), ("rollback_selected", "candidate", False),
    ("complete", "candidate", True), ("rolled_back", "old", True),
    ("unrecognized", "old", False),
])
def test_generated_command_pending_journal_admission_table(tmp_path, monkeypatch, step, selected, allowed):
    root = tmp_path / "program"
    data = tmp_path / "data"
    versions = {}
    for name in ("old", "candidate"):
        folder = root / "versions" / name
        folder.mkdir(parents=True)
        version = Version(folder, folder / "py/bin/python", folder / "profile.json", {},
                          3 if name == "old" else 4)
        write_json(folder / "version.json", version.document())
        versions[name] = version
    (root / "current").write_text(selected + "\n")
    if step is not None:
        write_json(root / "update-transaction.json", {
            "format": "astrid-install-update/v1", "operation_id": "f" * 32,
            "transaction_kind": "migration", "step": step,
            "data_root": str(data), "realm": {"realm_id": "realm", "realm_root": str(data / "realm")},
            "old": versions["old"].document(), "candidate": versions["candidate"].document(),
            # Historical extras cannot admit the wrong phase or version.
            "launch_intent": {"version": versions[selected].document()},
            "stop_authority": {"anything": "legacy"},
        })
    calls = []
    runtime = ModuleType("astrid_runtime.cli")
    runtime.main = lambda argv: calls.append(argv) or 0
    monkeypatch.setitem(sys.modules, "astrid_runtime.cli", runtime)
    generated = command(versions[selected], data)
    assert generated[1:5] == ["-X", "utf8", "-I", "-c"]
    if allowed:
        with pytest.raises(SystemExit) as result:
            exec(generated[-1], {})
        assert result.value.code == 0
        assert len(calls) == 1
    else:
        with pytest.raises(ValueError, match="An Astrid update was interrupted"):
            exec(generated[-1], {})
        assert calls == []
    assert not data.exists()


@pytest.mark.parametrize("step", ["switched", "complete", "rolled_back"])
@pytest.mark.parametrize("mutation", [
    "json", "empty", "format", "kind", "data_root", "version_root",
    "python", "profile", "identity", "release", "env", "operation_id", "realm",
])
def test_generated_command_rejects_malformed_or_mismatched_journal_before_runtime(
        tmp_path, monkeypatch, step, mutation):
    root, data = tmp_path / "program", tmp_path / "data"
    folder = root / "versions" / "old"
    folder.mkdir(parents=True)
    version = Version(folder, folder / "py/bin/python", folder / "profile.json", {}, 7)
    write_json(folder / "version.json", version.document())
    (root / "current").write_text("old\n")
    transaction = {
        "format": "astrid-install-update/v1", "operation_id": "a" * 32,
        "transaction_kind": "app", "step": step, "data_root": str(data),
        "realm": {"realm_id": "realm", "realm_root": str(data / "realm")},
        "old": version.document(), "candidate": version.document(),
    }
    if mutation in {"format", "kind", "data_root", "operation_id", "realm"}:
        key = "transaction_kind" if mutation == "kind" else mutation
        transaction[key] = "invalid"
    elif mutation in {"version_root", "python", "profile"}:
        key = "root" if mutation == "version_root" else mutation
        transaction["old"][key] = str(tmp_path / "foreign")
    elif mutation == "identity":
        transaction["old"]["identity"] = {"release": True, "managed_by": None}
    elif mutation == "release":
        transaction["old"]["identity"]["release"] = 999
    elif mutation == "env":
        transaction["old"]["env"] = {"PATH": 99}
    journal = root / "update-transaction.json"
    write_json(journal, {} if mutation == "empty" else transaction)
    if mutation == "json":
        journal.write_text("{broken")
    calls = []
    runtime = ModuleType("astrid_runtime.cli")
    runtime.main = lambda argv: calls.append(argv) or 0
    monkeypatch.setitem(sys.modules, "astrid_runtime.cli", runtime)
    with pytest.raises(ValueError, match="An Astrid update was interrupted"):
        exec(command(version, data)[-1], {})
    assert calls == []
    assert not data.exists()


@pytest.mark.parametrize("form", ["legacy", "canonical", "unknown"])
def test_windows_migrates_only_known_owned_task_action_before_pending_launch(tmp_path, monkeypatch, form):
    import subprocess

    root, data = tmp_path / "program", tmp_path / "data"
    folder = root / "versions" / "old"
    folder.mkdir(parents=True)
    version = Version(folder, folder / "py/Scripts/python.exe", folder / "profile.json", {}, 7)
    write_json(folder / "version.json", version.document())
    (root / "current").write_text("old\n")
    manager = Service(root, tmp_path / "log", Mock(), home=tmp_path, system="win32")
    write_json(manager.selection, {"owner": LABEL, "program_root": str(root), "data_root": str(data)})
    write_json(root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "a" * 32,
        "transaction_kind": "migration", "step": "migrated", "data_root": str(data),
        "realm": {"realm_id": "realm", "realm_root": str(data / "realm")},
        "old": version.document(), "candidate": version.document(),
    })
    # Fixture is the literal unbound wrapper emitted before X1b. The product
    # migration must recognize it without executing an old package's code.
    legacy = (
        "import json,os,sys\nfrom pathlib import Path\n"
        f"os.environ.update({version.env!r})\n"
        "os.environ.pop('ASTRID_INSTALLER_OPERATION_ID',None)\n"
        f"sys.stdout=sys.stderr=open({str(manager.log)!r},'a',encoding='utf-8',buffering=1)\n"
        f"path=Path({str(manager.env_file)!r})\n"
        "settings=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}\n"
        "if (not isinstance(settings,dict) or set(settings)-set(['ASTRID_ALLOW_UNQUALIFIED_GPU', 'ASTRID_WARM_IDLE_SECONDS']) "
        "or any(not isinstance(value,str) for value in settings.values())):\n"
        "    raise ValueError('Invalid Astrid service-env.json; use the documented string settings.')\n"
        "os.environ.update(settings)\n"
        f"from astrid_runtime.cli import main\nsys.exit(main({['run', '--profile', 'astrid', '--data-root', str(data), '--source-manifest', str(version.profile)]!r}))"
    )
    canonical = command(version, data, windows=True, log=manager.log, env_file=manager.env_file)
    task = ET.fromstring(render("win32", version, data, manager.log, user="test-user", env_file=manager.env_file))
    ns = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    arguments_node = task.find(".//task:Actions/task:Exec/task:Arguments", ns)
    arguments_node.text = subprocess.list2cmdline(canonical[1:] if form == "canonical" else [*canonical[1:-1], legacy])
    stored = [{
        "Execute": task.findtext(".//task:Actions/task:Exec/task:Command", namespaces=ns),
        "Arguments": arguments_node.text,
    }]
    saved_settings = ET.tostring(task.find("task:Settings", ns))
    if form == "unknown":
        stored[0]["Execute"] = str(tmp_path / "foreign.exe")
    writes = []

    def powershell(script):
        if "Set-ScheduledTask" in script:
            writes.append(script)
            assert "-cne" in script  # The same operation rechecks observed action.
            assert "New-ScheduledTaskAction" in script
            assert "Register-ScheduledTask" not in script
            stored[:] = [{"Execute": canonical[0], "Arguments": subprocess.list2cmdline(canonical[1:])}]
            return ""
        return json.dumps(stored, separators=(",", ":"))

    monkeypatch.setattr(manager, "powershell", powershell)
    if form == "unknown":
        with pytest.raises(ValueError, match="Astrid Runtime scheduled task.*install line"):
            manager.publish_launch_admission(version, data)
        assert writes == []
        assert not manager.path.exists()
        return
    manager.publish_launch_admission(version, data)
    assert len(writes) == (1 if form == "legacy" else 0)
    manager.publish_launch_admission(version, data)
    assert len(writes) == (1 if form == "legacy" else 0)  # Idempotent no-op.
    runtime = ModuleType("astrid_runtime.cli")
    runtime.main = lambda argv: pytest.fail("pending migrated launch entered Runtime")
    monkeypatch.setitem(sys.modules, "astrid_runtime.cli", runtime)
    # The registered canonical replacement refuses before Runtime/log writes.
    with pytest.raises(ValueError, match="An Astrid update was interrupted"):
        exec(canonical[-1], {})
    assert ET.tostring(task.find("task:Settings", ns)) == saved_settings
    assert not manager.log.exists()
    assert not data.exists()


def _legacy_posix_action(version, data, env_file, *, operation=None):
    """Frozen pre-X1b command format, independent of today's builder."""
    script = ("import json,os,sys\nfrom pathlib import Path\n"
              f"os.environ.update({version.env!r})\n")
    if operation:
        script += (
            f"_astrid_operation_id={operation!r}\n"
            f"_astrid_version={version.document()!r}\n"
            f"_astrid_transaction_path=Path({str(version.root.parent.parent / 'update-transaction.json')!r})\n"
            "try:\n"
            "    _astrid_tx=json.loads(_astrid_transaction_path.read_text(encoding='utf-8'))\n"
            "    _astrid_intent=_astrid_tx.get('launch_intent') or {}\n"
            "    _astrid_authorized=(_astrid_tx.get('step') not in {'complete','rolled_back'} "
            "and _astrid_tx.get('operation_id') == _astrid_operation_id "
            "and _astrid_intent.get('operation_id') == _astrid_operation_id "
            "and _astrid_intent.get('purpose') in {'backup','migration','service_start','pinokio_start'} "
            "and _astrid_intent.get('version') == _astrid_version "
            "and _astrid_version in (_astrid_tx.get('old'),_astrid_tx.get('candidate')))\n"
            "except (OSError,ValueError,TypeError,AttributeError):\n"
            "    _astrid_authorized=False\n"
            "if _astrid_authorized: os.environ['ASTRID_INSTALLER_OPERATION_ID']=_astrid_operation_id\n"
            "else: os.environ.pop('ASTRID_INSTALLER_OPERATION_ID',None)\n"
        )
    else:
        script += "os.environ.pop('ASTRID_INSTALLER_OPERATION_ID',None)\n"
    script += (
        f"path=Path({str(env_file)!r})\n"
        "settings=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}\n"
        "if (not isinstance(settings,dict) or set(settings)-set(['ASTRID_ALLOW_UNQUALIFIED_GPU', 'ASTRID_WARM_IDLE_SECONDS']) "
        "or any(not isinstance(value,str) for value in settings.values())):\n"
        "    raise ValueError('Invalid Astrid service-env.json; use the documented string settings.')\n"
        "os.environ.update(settings)\n"
        f"from astrid_runtime.cli import main\nsys.exit(main({['run', '--profile', 'astrid', '--data-root', str(data), '--source-manifest', str(version.profile)]!r}))"
    )
    return [str(version.python), "-X", "utf8", "-I", "-c", script]


def _posix_action_fixture(tmp_path, monkeypatch, system, form):
    import plistlib

    from astrid.installer.service import systemd_quote

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root, data = tmp_path / "Astrid $ % café", tmp_path / "data"
    folder = root / "versions" / "old"
    folder.mkdir(parents=True)
    # Preserve historical insertion order, which differs from sorted version.json.
    version = Version(folder, folder / "py/bin/python", folder / "profile.json", {"Z": "last", "A": "first"}, 7)
    write_json(folder / "version.json", version.document())
    (root / "current").write_text("old\n")
    run = Mock(return_value="")
    manager = Service(root, tmp_path / "log", run, home=tmp_path, system=system)
    write_json(manager.selection, {"owner": LABEL, "program_root": str(root), "data_root": str(data),
                                   "preferences": {"autostart": False}, "unrelated": "preserved"})
    transaction = {
        "format": "astrid-install-update/v1", "operation_id": "a" * 32,
        "transaction_kind": "migration", "step": "migrated", "data_root": str(data),
        "realm": {"realm_id": "realm", "realm_root": str(data / "realm")},
        "old": version.document(), "candidate": version.document(),
    }
    write_json(root / "update-transaction.json", transaction)
    canonical = command(version, data, env_file=manager.env_file)
    action = canonical if form == "canonical" else _legacy_posix_action(
        version, data, manager.env_file, operation=transaction["operation_id"] if form == "bound" else None)
    if form == "unknown":
        action[0] = str(tmp_path / "other-python")
    content = render(system, version, data, manager.log, env_file=manager.env_file)
    if system == "darwin":
        doc = plistlib.loads(content.encode())
        doc["ProgramArguments"] = action
        doc["RunAtLoad"] = False
        doc["ThrottleInterval"] = 17
        doc["EnvironmentVariables"]["USER_SETTING"] = "keep me"
        if form == "bound":
            doc["EnvironmentVariables"]["ASTRID_INSTALLER_OPERATION_ID"] = transaction["operation_id"]
        content = plistlib.dumps(doc).decode()
    else:
        lines = content.splitlines(keepends=True)
        content = "".join("ExecStart=" + " ".join(systemd_quote(arg).replace("$", "$$") for arg in action) + "\n"
                          if line.startswith("ExecStart=") else line for line in lines)
        properties = '# User service settings\nEnvironment="USER_SETTING=keep me"\nNice=7\n'
        if form == "bound":
            properties += 'Environment="ASTRID_INSTALLER_OPERATION_ID=' + transaction["operation_id"] + '"\n'
        content = content.replace("\n[Install]", "\n" + properties + "\n[Install]")
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    manager.path.write_text(content)
    manager.path.chmod(0o640)
    return manager, version, data, transaction, canonical


@pytest.mark.parametrize("system", ["linux", "darwin"])
@pytest.mark.parametrize("form", ["legacy", "bound", "canonical", "unknown"])
def test_posix_migrates_only_known_action_and_preserves_service_properties(tmp_path, monkeypatch, system, form):
    import plistlib

    from astrid.installer.service import systemd_quote

    manager, version, data, transaction, canonical = _posix_action_fixture(tmp_path, monkeypatch, system, form)
    before, record = manager.path.read_bytes(), manager.selection.read_bytes()
    if form == "unknown":
        with pytest.raises(ValueError, match="Astrid service.*install line"):
            manager.publish_launch_admission(version, data)
        assert manager.path.read_bytes() == before
        assert manager.run.call_args_list == []
        return
    manager.publish_launch_admission(version, data)
    published = manager.path.read_bytes()
    assert (published == before) == (form == "canonical")
    assert manager.path.stat().st_mode & 0o777 == 0o640
    assert manager.selection.read_bytes() == record
    if system == "darwin":
        old, new = plistlib.loads(before), plistlib.loads(published)
        assert new.pop("ProgramArguments") == canonical
        old.pop("ProgramArguments")
        old["EnvironmentVariables"].pop("ASTRID_INSTALLER_OPERATION_ID", None)
        assert new == old
        assert manager.run.call_args_list == []
    else:
        original = before.decode().splitlines(keepends=True)
        expected = "".join("ExecStart=" + " ".join(systemd_quote(arg).replace("$", "$$") for arg in canonical) + "\n"
                           if line.startswith("ExecStart=") else line for line in original
                           if not line.startswith('Environment="ASTRID_INSTALLER_OPERATION_ID='))
        assert published.decode() == expected
        assert manager.run.call_args_list[0].args[0] == ["systemctl", "--user", "show-environment"]
        assert manager.run.call_args_list[1].args[0] == ["systemctl", "--user", "daemon-reload"]
    manager.publish_launch_admission(version, data)
    assert manager.path.read_bytes() == published
    runtime = ModuleType("astrid_runtime.cli")
    calls = []
    runtime.main = lambda argv: calls.append(argv) or 0
    monkeypatch.setitem(sys.modules, "astrid_runtime.cli", runtime)
    # Execute the action read from the stored replacement, not a rebuilt command.
    if system == "darwin":
        stored = plistlib.loads(published)["ProgramArguments"]
    else:
        line = next(line for line in published.decode().splitlines() if line.startswith("ExecStart="))
        assert line == "ExecStart=" + " ".join(systemd_quote(arg).replace("$", "$$") for arg in canonical)
        stored = canonical
    with pytest.raises(ValueError, match="An Astrid update was interrupted"):
        exec(stored[-1], {})
    assert calls == []
    assert not data.exists()
    for step in ("switched", "rollback_selected"):
        transaction["step"] = step
        write_json(manager.root / "update-transaction.json", transaction)
        with pytest.raises(SystemExit) as exited:
            exec(stored[-1], {})
        assert exited.value.code == 0
    assert len(calls) == 2


@pytest.mark.parametrize("system", ["linux", "darwin"])
@pytest.mark.parametrize("mutation", ["owner", "symlink", "changed", "write_failure"])
def test_posix_action_publication_refuses_before_stop_or_unit_mutation(tmp_path, monkeypatch, system, mutation):
    import os
    from pathlib import Path

    manager, version, data, _transaction, _canonical = _posix_action_fixture(tmp_path, monkeypatch, system, "legacy")
    before = manager.path.read_bytes()
    if mutation == "owner":
        record = read_json(manager.selection)
        write_json(manager.selection, {**record, "program_root": str(tmp_path / "other-install")})
    elif mutation == "symlink":
        target = tmp_path / "other-service"
        target.write_bytes(before)
        manager.path.unlink()
        manager.path.symlink_to(target)
    elif mutation == "changed":
        original = Path.read_bytes
        reads = []
        def changed(path):
            result = original(path)
            if path == manager.path:
                reads.append(path)
                if len(reads) == 1:
                    path.write_bytes(before + b"\n# changed externally\n")
            return result
        monkeypatch.setattr(Path, "read_bytes", changed)
    else:
        def refused(*args):
            raise OSError("publication failed")
        monkeypatch.setattr(os, "replace", refused)
    with pytest.raises((ValueError, OSError), match="install line|publication failed"):
        manager.publish_launch_admission(version, data)
    assert manager.run.call_args_list == []  # No manager reload, start, signal or stop.
    assert manager.path.read_bytes() == before + (b"\n# changed externally\n" if mutation == "changed" else b"")
    assert not list(manager.path.parent.glob(".astrid-*"))
    assert not data.exists()


@pytest.mark.parametrize("mutation", ["second_action", "wrong_section", "combined_environment"])
def test_systemd_unrecognized_action_syntax_never_rewrites_settings(tmp_path, monkeypatch, mutation):
    manager, version, data, transaction, _canonical = _posix_action_fixture(tmp_path, monkeypatch, "linux", "legacy")
    text = manager.path.read_text()
    if mutation == "second_action":
        text = text.replace("[Service]\n", '[Service]\n ExecStart="/other/program"\n')
    elif mutation == "wrong_section":
        text = text.replace("[Service]", "[Unit]")
    else:
        text = text.replace("[Service]\n", '[Service]\nEnvironment="ASTRID_INSTALLER_OPERATION_ID='
                            + transaction["operation_id"] + '" "KEEP=setting"\n')
    manager.path.write_text(text)
    with pytest.raises(ValueError, match="Astrid service.*install line"):
        manager.publish_launch_admission(version, data)
    assert manager.path.read_text() == text
    assert manager.run.call_args_list == []


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_posix_action_directory_flush_failure_retries_without_rewriting(tmp_path, monkeypatch, system):
    from runtime_protocol import platform

    manager, version, data, _transaction, _canonical = _posix_action_fixture(tmp_path, monkeypatch, system, "legacy")
    original = manager.path.read_bytes()
    flush, observations = platform.fsync_directory, []
    def fail_once(parent):
        observations.append(parent)
        if len(observations) == 1:
            raise OSError("action directory flush failed")
        flush(parent)
    monkeypatch.setattr(platform, "fsync_directory", fail_once)
    with pytest.raises(OSError, match="action directory flush failed"):
        manager.publish_launch_admission(version, data)
    published = manager.path.read_bytes()
    assert published != original  # Rename committed; transaction still cannot stop.
    assert manager.run.call_args_list == []
    before = manager.path.stat()
    manager.publish_launch_admission(version, data)
    after = manager.path.stat()
    assert manager.path.read_bytes() == published
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert len(observations) == 2  # Retry completes durability, including no-op actions.
