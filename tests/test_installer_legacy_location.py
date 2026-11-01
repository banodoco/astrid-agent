"""Location binding for selected identityless installer records."""
import json
import os
import sys
from pathlib import Path

import pytest

from runtime_protocol import platform
from astrid.installer import cli, foreground, local_update, update_job
from astrid.installer.core import Version, current, write_json
from astrid.installer.service import LABEL, installer_lock_path

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="XDG location ruling")


def legacy(home, root, *, bound=False):
    folder = root / "versions" / "old"
    version = Version(folder, folder / "bin/python", folder / "profile.json", {}, managed_by="pinokio")
    record = version.document()
    if not bound:
        record.pop("identity")
    write_json(folder / "version.json", record)
    write_json(folder / "manifest.json", {"release": 0})
    write_json(root / "install.json", {"owner": LABEL, "managed_by": "pinokio", "data_root": str(home / "data")})
    (root / "current").write_text("old\n")
    return folder / "version.json"


def snapshot(paths):
    return [(p.stat().st_ino, p.read_bytes()) for p in paths]


@pytest.fixture
def setup_location(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    return home


def test_owned_canonical_legacy_uses_existing_lock(setup_location, monkeypatch):
    home = setup_location
    root = home / ".local/share/astrid"
    record = legacy(home, root)
    lock = installer_lock_path(home, "linux")
    lock.write_bytes(b"existing lock")
    before = snapshot([record, lock])
    assert current(root).root == root / "versions/old"
    with lock.open("a+b") as held:
        platform.lock_file(held.fileno(), blocking=False)
        with pytest.raises(ValueError, match="already running or being installed"):
            foreground.launch(root, [])
        platform.unlock_file(held.fileno())
    assert snapshot([record, lock]) == before


@pytest.mark.parametrize("location", ["custom-root", "custom-xdg", "canonical-with-custom-xdg", "wrong-owner"])
@pytest.mark.parametrize("entry", ["foreground", "cli-install", "cli-update", "cli-uninstall",
                                   "cli-app-update", "worker", "local", "custody", "preflight"])
def test_identityless_refusal_is_durable_and_does_not_touch_lock_or_metadata(
        setup_location, monkeypatch, capsys, location, entry):
    home = setup_location
    default = home / ".local/share/astrid"
    root = home / "other/astrid" if location == "custom-root" else default
    if location == "custom-xdg":
        monkeypatch.setenv("XDG_DATA_HOME", str(home / "custom-xdg"))
        root = home / "custom-xdg/astrid"
    if location == "canonical-with-custom-xdg":
        monkeypatch.setenv("XDG_DATA_HOME", str(home / "custom-xdg"))
    record = legacy(home, root)
    lock = installer_lock_path(home, "linux")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"held lock")
    metadata = [record, root / "install.json", root / "current", lock]
    before = snapshot(metadata)
    if location == "wrong-owner":
        original = platform.is_current_user
        bad_inode = record.stat().st_ino
        monkeypatch.setattr(platform, "is_current_user", lambda st: st.st_ino != bad_inode and original(st))
    monkeypatch.setattr(os, "execvpe", lambda *_: pytest.fail("Runtime executed"))
    monkeypatch.setattr(platform, "lock_file", lambda *_args, **_kwargs: pytest.fail("lock opened"))
    if entry.startswith("cli-"):
        mode = {"cli-install": [], "cli-update": ["--update"],
                "cli-uninstall": ["--uninstall"],
                "cli-app-update": ["--update-app", "--managed-by", "pinokio"]}[entry]
        assert cli.main(["--root", str(root), *mode]) == 1
        assert "Unsupported legacy location. Reinstall fresh." in capsys.readouterr().out
    else:
        with pytest.raises(ValueError, match="Unsupported legacy location. Reinstall fresh"):
            if entry == "foreground":
                foreground.launch(root, [])
            elif entry == "worker":
                update_job._locked(root)
            elif entry == "local":
                local_update.execute({"action": "update"}, root, stdin=None, stdout=None)
            elif entry == "custody":
                from astrid.installer.custody import operation

                with operation(root, None):
                    pytest.fail("custody admitted")
            else:
                from astrid.installer.app_update import preflight_pinokio_update

                preflight_pinokio_update(root, None, home / "data")
    receipt = json.loads((platform.log_dir() / "unsupported-legacy-location.json").read_text())
    assert receipt == {"exit": "unsupported-legacy-location", "message": "Unsupported legacy location. Reinstall fresh.", "program_root": str(root)}
    assert snapshot(metadata) == before


def test_bound_custom_location_keeps_existing_behavior(setup_location):
    home = setup_location
    root = home / "custom/astrid"
    legacy(home, root, bound=True)
    assert current(root).root == root / "versions/old"
    assert not (platform.log_dir() / "unsupported-legacy-location.json").exists()


def test_custom_legacy_refusal_preserves_a_genuinely_held_lock(setup_location):
    home = setup_location
    root = home / "outside/astrid"
    record = legacy(home, root)
    lock = installer_lock_path(home, "linux")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"the original inode")
    before = snapshot([record, lock])
    with lock.open("a+b") as held:
        platform.lock_file(held.fileno(), blocking=False)
        with pytest.raises(ValueError, match="Unsupported legacy location"):
            foreground.launch(root, [])
        with lock.open("a+b") as contender:
            with pytest.raises(BlockingIOError):
                platform.lock_file(contender.fileno(), blocking=False)
        assert snapshot([record, lock]) == before
        platform.unlock_file(held.fileno())


def test_generated_foreground_refuses_legacy_before_runtime(setup_location, monkeypatch, tmp_path):
    import subprocess
    from astrid.installer.launcher import publish_runtime_runner

    home = setup_location
    root = home / "outside/astrid"
    record = legacy(home, root)
    lock = installer_lock_path(home, "linux")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"original")
    runner = publish_runtime_runner(root)
    before = snapshot([record, lock])
    env = dict(os.environ, XDG_STATE_HOME=str(tmp_path / "state"))
    result = subprocess.run([sys.executable, str(runner)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "Unsupported legacy location. Reinstall fresh." in result.stderr
    receipt = tmp_path / "state/astrid-runtime/logs/unsupported-legacy-location.json"
    assert json.loads(receipt.read_text())["exit"] == "unsupported-legacy-location"
    assert snapshot([record, lock]) == before


@pytest.mark.parametrize("relative", [".local", ".local/share", ".local/share/astrid", ".local/share/astrid/current", ".local/share/astrid/install.json", ".local/share/astrid/versions/old/manifest.json", ".local/share/astrid-install.lock", ".local/share/astrid-install.json"])
def test_each_default_binding_path_must_be_owned(setup_location, monkeypatch, relative):
    home = setup_location
    root = home / ".local/share/astrid"
    legacy(home, root)
    lock = installer_lock_path(home, "linux")
    lock.write_bytes(b"lock")
    selection = home / ".local/share/astrid-install.json"
    selection.write_text("{}")
    target = home / relative
    target_inode = target.stat().st_ino
    original = platform.is_current_user
    monkeypatch.setattr(platform, "is_current_user", lambda st: st.st_ino != target_inode and original(st))
    before = snapshot([root / "versions/old/version.json", root / "install.json", lock, selection])
    with pytest.raises(ValueError, match="Unsupported legacy location"):
        current(root)
    assert snapshot([root / "versions/old/version.json", root / "install.json", lock, selection]) == before
    assert json.loads((platform.log_dir() / "unsupported-legacy-location.json").read_text())["exit"] == "unsupported-legacy-location"


def test_worker_rechecks_after_acquiring_existing_lock(setup_location, monkeypatch):
    from astrid.installer import core

    home = setup_location
    root = home / ".local/share/astrid"
    legacy(home, root)
    lock = installer_lock_path(home, "linux")
    lock.write_bytes(b"same lock")
    before = snapshot([lock])
    calls = []

    def admission(_root):
        calls.append(_root)
        if len(calls) == 2:
            raise ValueError("changed while acquiring")

    monkeypatch.setattr(core, "legacy_location_admission", admission)
    with pytest.raises(ValueError, match="changed while acquiring"):
        update_job._locked(root)
    assert len(calls) == 2
    assert snapshot([lock]) == before
    with lock.open("a+b") as stream:
        platform.lock_file(stream.fileno(), blocking=False)
        platform.unlock_file(stream.fileno())
