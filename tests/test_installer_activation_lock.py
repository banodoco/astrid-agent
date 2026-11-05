"""Activation must bind the journal to the installer's independently held lock."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer.core import Installer, Version, write_json
from astrid.installer.service import command, installer_lock_path
from runtime_protocol import platform


@pytest.fixture
def installation(tmp_path, monkeypatch):
    home = tmp_path / "installer-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "custom-xdg"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "custom-localappdata"))
    root, data = tmp_path / "custom-program", tmp_path / "data"
    versions = []
    for name in ("old", "candidate"):
        folder = root / "versions" / name
        folder.mkdir(parents=True)
        version = Version(folder, folder / "py/bin/python", folder / "profile.json", {}, 2)
        write_json(folder / "version.json", version.document())
        versions.append(version)
    (root / "current").write_text("candidate\n")
    journal = {"format": "astrid-install-update/v1", "operation_id": "a" * 32,
               "transaction_kind": "migration", "data_root": str(data),
               "realm": {"realm_id": "realm", "realm_root": str(data / "realm")},
               "old": versions[0].document(), "candidate": versions[1].document()}
    lock = installer_lock_path(home, sys.platform)
    platform.private_directory(lock.parent)
    backend = SimpleNamespace(service=SimpleNamespace(activation_lock_path=lock))
    installer = Installer(root, data, backend)
    return SimpleNamespace(root=root, data=data, old=versions[0], candidate=versions[1],
                           journal=journal, lock=lock, installer=installer, tmp=tmp_path)


def _service(env, version, *, home, bound=True):
    # Execute the generated action in a separate process with a different HOME
    # and XDG_DATA_HOME. Runtime itself is a stub; the admission prelude is real.
    script = command(version, env.data, expected_lock=env.lock)[-1]
    if not bound:
        script = script.replace(f",expected_lock=Path({str(env.lock)!r})", "", 1)
    stub = ("import sys,types\n"
            "m=types.ModuleType('astrid_runtime.cli')\n"
            "m.main=lambda argv: 0\n"
            "sys.modules['astrid_runtime.cli']=m\n")
    child_env = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
                     XDG_DATA_HOME=str(home / "xdg"), LOCALAPPDATA=str(home / "local"))
    return subprocess.run([sys.executable, "-c", stub + script], env=child_env,
                          text=True, capture_output=True, check=False)


@pytest.mark.parametrize("step,selected", [("switched", "candidate"), ("rollback_selected", "old")])
def test_generated_service_uses_lock_held_by_installer_across_homes(installation, step, selected):
    env = installation
    (env.root / "current").write_text(selected + "\n")
    env.installer._save_update(env.journal, step)
    assert env.journal["transaction_context"]["activation_lock_path"] == str(env.lock)
    version = getattr(env, selected)
    with env.lock.open("a+b") as lock:
        platform.lock_file(lock.fileno(), blocking=False)
        try:
            result = _service(env, version, home=env.tmp / "service-home")
            assert result.returncode == 0, result.stderr
        finally:
            platform.unlock_file(lock.fileno())
    result = _service(env, version, home=env.tmp / "service-home")
    assert result.returncode != 0
    assert "Choose Update to finish" in result.stderr


def test_journal_and_service_use_captured_lock_if_installer_environment_changes(installation, monkeypatch):
    env = installation
    monkeypatch.setenv("HOME", str(env.tmp / "changed-installer-home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(env.tmp / "changed-xdg"))
    monkeypatch.setenv("LOCALAPPDATA", str(env.tmp / "changed-local"))
    with env.lock.open("a+b") as lock:
        platform.lock_file(lock.fileno(), blocking=False)
        try:
            env.installer._save_update(env.journal, "switched")
            assert env.journal["transaction_context"]["activation_lock_path"] == str(env.lock)
            result = _service(env, env.candidate, home=env.tmp / "service-home")
            assert result.returncode == 0, result.stderr
        finally:
            platform.unlock_file(lock.fileno())


@pytest.mark.parametrize("bad", ["relative", "other", "null", "noncanonical"])
def test_tampered_journal_path_does_not_probe_another_held_lock(installation, bad):
    env = installation
    env.installer._save_update(env.journal, "switched")
    other = env.tmp / "other" / "astrid-install.lock"
    platform.private_directory(other.parent)
    paths = {"relative": "astrid-install.lock", "other": str(other), "null": None,
             "noncanonical": str(env.lock.parent) + "/../" + env.lock.parent.name + "/astrid-install.lock"}
    env.journal["transaction_context"]["activation_lock_path"] = paths[bad]
    write_json(env.root / "update-transaction.json", env.journal)
    with other.open("a+b") as lock:
        platform.lock_file(lock.fileno(), blocking=False)
        try:
            result = _service(env, env.candidate, home=env.tmp / "service-home")
        finally:
            platform.unlock_file(lock.fileno())
    assert result.returncode != 0
    assert "Invalid activation lock path" in result.stderr


@pytest.mark.parametrize("step,selected", [("switched", "candidate"), ("rollback_selected", "old")])
@pytest.mark.parametrize("tamper", ["delete-field", "delete-context", "null-context", "list-context", "false-context"])
def test_bound_action_refuses_missing_or_malformed_context(installation, step, selected, tamper):
    env = installation
    (env.root / "current").write_text(selected + "\n")
    env.installer._save_update(env.journal, step)
    if tamper == "delete-field":
        del env.journal["transaction_context"]["activation_lock_path"]
    elif tamper == "delete-context":
        del env.journal["transaction_context"]
    else:
        env.journal["transaction_context"] = {"null-context": None, "list-context": [], "false-context": False}[tamper]
    journal = env.root / "update-transaction.json"
    write_json(journal, env.journal)
    before = journal.read_bytes()
    data = env.tmp / "retained-data"
    data.write_bytes(b"data-kept")
    service_home = env.tmp / "service-home"
    if sys.platform == "win32":
        service_lock = service_home / "local/Astrid Installer/astrid-install.lock"
    elif sys.platform == "linux":
        service_lock = service_home / "xdg/astrid-install.lock"
    else:
        service_lock = service_home / ".local/share/astrid-install.lock"
    platform.private_directory(service_lock.parent)
    with service_lock.open("a+b") as lock:
        platform.lock_file(lock.fileno(), blocking=False)
        try:
            assert _service(env, getattr(env, selected), home=service_home).returncode != 0
        finally:
            platform.unlock_file(lock.fileno())
    assert journal.read_bytes() == before
    assert data.read_bytes() == b"data-kept"


def test_bound_action_refuses_missing_binding_even_when_expected_lock_is_held(installation):
    env = installation
    env.journal["step"] = "switched"
    write_json(env.root / "update-transaction.json", env.journal)
    with env.lock.open("a+b") as lock:
        platform.lock_file(lock.fileno(), blocking=False)
        try:
            assert _service(env, env.candidate, home=env.tmp / "service-home").returncode != 0
        finally:
            platform.unlock_file(lock.fileno())


def test_unbound_legacy_journal_retains_service_home_fallback(installation):
    env = installation
    env.journal["step"] = "switched"
    write_json(env.root / "update-transaction.json", env.journal)
    result = _service(env, env.candidate, home=env.tmp / "service-home", bound=False)
    assert result.returncode != 0 and "Choose Update to finish" in result.stderr
    with env.lock.open("a+b") as lock:
        platform.lock_file(lock.fileno(), blocking=False)
        try:
            # Legacy journals still probe the service process's HOME/XDG.
            result = _service(env, env.candidate, home=env.tmp / "service-home", bound=False)
            assert result.returncode != 0
        finally:
            platform.unlock_file(lock.fileno())
    service_home = env.tmp / "service-home"
    if sys.platform == "win32":
        service_lock = service_home / "local" / "Astrid Installer" / "astrid-install.lock"
    elif sys.platform == "linux":
        service_lock = service_home / "xdg" / "astrid-install.lock"
    else:
        service_lock = service_home / ".local" / "share" / "astrid-install.lock"
    with service_lock.open("a+b") as lock:
        platform.lock_file(lock.fileno(), blocking=False)
        try:
            assert _service(env, env.candidate, home=env.tmp / "service-home", bound=False).returncode == 0
        finally:
            platform.unlock_file(lock.fileno())


@pytest.mark.parametrize("context", [None, [], False, "", 0])
def test_unbound_action_rejects_malformed_present_context(installation, context):
    env = installation
    env.journal.update(step="switched", transaction_context=context)
    write_json(env.root / "update-transaction.json", env.journal)
    result = _service(env, env.candidate, home=env.tmp / "service-home", bound=False)
    assert result.returncode != 0
    assert "Invalid update transaction context" in result.stderr


def test_pinokio_accepts_only_its_root_lock(tmp_path):
    from astrid.installer.service import activation_lock_held

    root = tmp_path / "program"
    root.mkdir()
    version = Version(root / "versions" / "v", root / "versions" / "v" / "python",
                      root / "versions" / "v" / "profile", {}, 1, "pinokio")
    path = root / ".app-update.lock"
    other = tmp_path / "other.lock"
    with path.open("a+b") as lock:
        platform.lock_file(lock.fileno(), blocking=False)
        try:
            assert activation_lock_held(root, version, {"activation_lock_path": str(path)})
            with pytest.raises(ValueError, match="Invalid activation lock path"):
                activation_lock_held(root, version, {"activation_lock_path": str(other)})
        finally:
            platform.unlock_file(lock.fileno())
    assert not activation_lock_held(root, version, {"activation_lock_path": str(path)})
