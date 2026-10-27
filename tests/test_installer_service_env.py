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


def test_installer_runtime_command_sets_utf8_before_isolation(tmp_path, monkeypatch):
    backend = Backend(tmp_path / "program", tmp_path / "log", tmp_path / "uv")
    version = Version(tmp_path / "version", tmp_path / "python", tmp_path / "profile", {})
    run = Mock(return_value='{"ok":true}')
    monkeypatch.setattr(backend, "run", run)

    backend.runtime(version, tmp_path / "data", "status")

    assert run.call_args.args[0][:6] == [
        version.python, "-X", "utf8", "-I", "-m", "astrid_runtime",
    ]


def test_transaction_runtime_launch_binds_operation_and_saves_authorized_intent(tmp_path):
    from astrid.installer import core

    backend = Backend(tmp_path / "program", tmp_path / "log", tmp_path / "uv")
    version = Version(tmp_path / "version", tmp_path / "python", tmp_path / "profile", {})
    operation_id = "a" * 32
    transaction = {"operation_id": operation_id, "step": "backed_up"}
    calls = []

    def run(argv, *, extra_env=None, **_kwargs):
        calls.append((argv, extra_env))
        return '{"ok":true}'

    backend.run = run
    with core.transaction_stop_context(
        backend, transaction,
        lambda step, **details: transaction.update(step=step, **details),
    ):
        backend.runtime(version, tmp_path / "data", "up", profile=True)

    assert calls[0][1]["ASTRID_INSTALLER_OPERATION_ID"] == operation_id
    assert transaction["launch_intent"] == {
        "operation_id": operation_id, "purpose": "backup", "version": version.document(),
    }
    assert "ASTRID_INSTALLER_OPERATION_ID" not in version.env


def test_transaction_binding_is_present_in_rendered_service_and_wrapper(tmp_path, monkeypatch):
    from astrid.installer.core import write_json

    operation_id = "b" * 32
    version = Version(tmp_path / "program" / "versions" / "v1", tmp_path / "python", tmp_path / "profile", {})
    settings = tmp_path / "service-env.json"
    write_json(settings, {"ASTRID_WARM_IDLE_SECONDS": "17"})
    rendered = render("linux", version, tmp_path / "data", tmp_path / "log",
                      env_file=settings, installer_operation_id=operation_id)
    assert f'Environment="ASTRID_INSTALLER_OPERATION_ID={operation_id}"' in rendered
    assert "ASTRID_INSTALLER_OPERATION_ID" not in read_json(settings)
    write_json(tmp_path / "program" / "update-transaction.json", {
        "step": "switched", "operation_id": operation_id,
        "old": version.document(), "candidate": version.document(),
        "launch_intent": {"operation_id": operation_id, "purpose": "service_start",
                          "version": version.document()},
    })

    observed = []
    module = ModuleType("astrid_runtime.cli")
    def main(argv):
        import os
        observed.append((argv, os.environ.get("ASTRID_INSTALLER_OPERATION_ID")))
        return 0
    module.main = main
    monkeypatch.setitem(sys.modules, "astrid_runtime.cli", module)
    argv = command(version, tmp_path / "data", env_file=settings,
                   installer_operation_id=operation_id)
    with pytest.raises(SystemExit) as result:
        exec(argv[-1], {})
    assert result.value.code == 0
    assert observed[0][1] == operation_id


def test_ordinary_service_wrapper_clears_inherited_transaction_binding(tmp_path, monkeypatch):
    version = Version(tmp_path / "version", tmp_path / "python", tmp_path / "profile", {})
    observed = []
    module = ModuleType("astrid_runtime.cli")
    def main(_argv):
        import os
        observed.append(os.environ.get("ASTRID_INSTALLER_OPERATION_ID"))
        return 0
    module.main = main
    monkeypatch.setitem(sys.modules, "astrid_runtime.cli", module)
    monkeypatch.setenv("ASTRID_INSTALLER_OPERATION_ID", "c" * 32)
    argv = command(version, tmp_path / "data", env_file=tmp_path / "missing.json")
    with pytest.raises(SystemExit):
        exec(argv[-1], {})
    assert observed == [None]


def test_stale_rendered_binding_is_cleared_after_transaction_terminal(tmp_path, monkeypatch):
    from astrid.installer.core import write_json

    operation_id = "e" * 32
    version = Version(tmp_path / "program" / "versions" / "v1", tmp_path / "python",
                      tmp_path / "profile", {})
    write_json(tmp_path / "program" / "update-transaction.json", {
        "step": "complete", "operation_id": operation_id,
        "old": version.document(), "candidate": version.document(),
        "launch_intent": {"operation_id": operation_id, "purpose": "service_start",
                          "version": version.document()},
    })
    observed = []
    module = ModuleType("astrid_runtime.cli")
    def main(_argv):
        import os
        observed.append(os.environ.get("ASTRID_INSTALLER_OPERATION_ID"))
        return 0
    module.main = main
    monkeypatch.setitem(sys.modules, "astrid_runtime.cli", module)
    argv = command(version, tmp_path / "data", installer_operation_id=operation_id)
    with pytest.raises(SystemExit):
        exec(argv[-1], {})
    assert observed == [None]


def test_windows_clear_updates_registered_task_action_without_starting_it(tmp_path, monkeypatch):
    from astrid.installer.core import write_json

    root = tmp_path / "program"
    manager = Service(root, tmp_path / "log", Mock(), home=tmp_path, system="win32")
    version = Version(root / "versions" / "v1", tmp_path / "pythonw.exe", tmp_path / "profile", {})
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    operation_id = "9" * 32
    write_json(manager.env_file, {})
    manager.path.write_text(render("win32", version, tmp_path / "data", tmp_path / "log",
                                   installer_operation_id=operation_id), encoding="utf-8")
    commands = []
    monkeypatch.setattr(manager, "powershell", lambda script: commands.append(script) or "")

    manager.clear_launch_operation_id(version, tmp_path / "data")

    assert operation_id not in manager.path.read_text(encoding="utf-8")
    assert any("Register-ScheduledTask" in command for command in commands)
    assert any(str(manager.path) in command for command in commands)


@pytest.mark.parametrize("bound", [True, False], ids=["operation-bound", "binding-cleared"])
def test_windows_stop_validation_accepts_exact_transaction_launch_actions(tmp_path, monkeypatch, bound):
    root = tmp_path / "program"
    manager = Service(root, tmp_path / "log", Mock(), home=tmp_path, system="win32")
    version = Version(root / "versions" / "candidate", tmp_path / "python.exe",
                      tmp_path / "profile.json", {})
    version.root.mkdir(parents=True)
    write_json(version.root / "version.json", version.document())
    operation_id = "f" * 32
    monkeypatch.setattr(manager, "powershell", lambda _script: "")
    manager.register(version, tmp_path / "data", installer_operation_id=operation_id,
                     refresh_only=True)
    if not bound:
        manager.clear_launch_operation_id(version, tmp_path / "data")
    xml_root = ET.fromstring(manager.path.read_text(encoding="utf-8"))
    namespace = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    actions = [{
        "Execute": xml_root.findtext(".//task:Actions/task:Exec/task:Command", namespaces=namespace),
        "Arguments": xml_root.findtext(".//task:Actions/task:Exec/task:Arguments", namespaces=namespace),
    }]
    monkeypatch.setattr(manager, "powershell", lambda _script: json.dumps(actions))

    manager.validate_stop_target(
        version, tmp_path / "data", owner_pid=None,
        installer_operation_id=operation_id, installer_launch_version=version,
    )


@pytest.mark.parametrize("mutation", ["binding", "version", "path", "argument"])
def test_windows_stop_validation_rejects_mutated_transaction_launch_action(tmp_path, monkeypatch, mutation):
    root = tmp_path / "program"
    manager = Service(root, tmp_path / "log", Mock(), home=tmp_path, system="win32")
    version = Version(root / "versions" / "candidate", tmp_path / "python.exe",
                      tmp_path / "profile.json", {})
    version.root.mkdir(parents=True)
    write_json(version.root / "version.json", version.document())
    operation_id = "a" * 32
    monkeypatch.setattr(manager, "powershell", lambda _script: "")
    manager.register(version, tmp_path / "data", installer_operation_id=operation_id,
                     refresh_only=True)
    transaction_path = root / "update-transaction.json"
    write_json(transaction_path, {
        "step": "switched", "operation_id": operation_id,
        "old": version.document(), "candidate": version.document(),
        "launch_intent": {"operation_id": operation_id, "purpose": "service_start",
                          "version": version.document()},
    })
    xml_root = ET.fromstring(manager.path.read_text(encoding="utf-8"))
    namespace = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    action = {
        "Execute": xml_root.findtext(".//task:Actions/task:Exec/task:Command", namespaces=namespace),
        "Arguments": xml_root.findtext(".//task:Actions/task:Exec/task:Arguments", namespaces=namespace),
    }
    if mutation == "binding":
        action["Arguments"] = action["Arguments"].replace(operation_id, "b" * 32)
    elif mutation == "version":
        action["Execute"] = str(tmp_path / "other-python.exe")
    elif mutation == "path":
        action["Arguments"] = action["Arguments"].replace(str(version.profile), str(tmp_path / "other-profile.json"))
    else:
        action["Arguments"] += " --unexpected"
    monkeypatch.setattr(manager, "powershell", lambda _script: json.dumps([action]))
    before = {
        "task": manager.path.read_bytes(),
        "selection": manager.selection.read_bytes(),
        "journal": transaction_path.read_bytes(),
    }

    with pytest.raises(ValueError, match="scheduled task does not match"):
        manager.validate_stop_target(
            version, tmp_path / "data", owner_pid=None,
            installer_operation_id=operation_id, installer_launch_version=version,
        )

    assert manager.path.read_bytes() == before["task"]
    assert manager.selection.read_bytes() == before["selection"]
    assert transaction_path.read_bytes() == before["journal"]


@pytest.mark.parametrize("release,managed_by", [(0, None), (37, "pinokio")])
def test_service_wrapper_loads_settings_before_runtime(tmp_path, monkeypatch, release, managed_by):
    settings = tmp_path / "service-env.json"
    write_json(settings, {"ASTRID_WARM_IDLE_SECONDS": "17", "ASTRID_ALLOW_UNQUALIFIED_GPU": "1"})
    monkeypatch.setenv("ASTRID_WARM_IDLE_SECONDS", "900")
    monkeypatch.setenv("ASTRID_ALLOW_UNQUALIFIED_GPU", "0")
    version = Version(tmp_path / "version", tmp_path / "python", tmp_path / "profile", {},
                      release, managed_by)
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
    version = Version(tmp_path / "version", tmp_path / "python", tmp_path / "profile", {})
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
    })
    manager.workspace = lambda *_args: {"realm_id": "realm", "realm_root": str(data / "realm")}
    manager.verify = lambda *_args, **_kwargs: None
    manager.restart_required = lambda: False
    manager.open = lambda: None
    manager.service.register = lambda *_args: pytest.fail("healthy repair must not re-register")

    from astrid.installer.core import Installer
    Installer(root, data, manager, say=lambda _line: None).run("install", {})

    assert json.loads((version_root / "version.json").read_text())["identity"] == {
        "release": 19, "managed_by": None,
    }
    selected = read_json(manager.service.selection)
    assert selected["identity"] == {"release": 19, "managed_by": None}
    assert selected["unrelated"] == {"preserve": True}


@pytest.mark.parametrize("managed_by", [None, "pinokio"])
def test_backend_registration_keeps_selection_fields_and_records_selected_identity(tmp_path, monkeypatch, managed_by):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    root, data = tmp_path / "program", tmp_path / "data"
    version_root = root / "versions" / "v1"
    version_root.mkdir(parents=True)
    version = Version(version_root, tmp_path / "python", tmp_path / "profile", {}, 29, managed_by)
    backend = Backend(root, tmp_path / "log", tmp_path / "uv", managed_by=managed_by)
    write_json(backend.service.selection, {"owner": LABEL, "unrelated": "retained", "uninstalled": True})
    backend.service.register = lambda *_args: None
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
