"""The public installer recovers durable updates before consulting fresh inputs."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from runtime_protocol import platform

from astrid.installer import app_update, cli
from astrid.installer.backend import Backend
from astrid.installer.core import Installer, Version, current, read_json, switch, write_json
from astrid.installer.service import LABEL


def _cli_backend(tmp_path, root, data, *, managed_by=None):
    record = {
        "owner": LABEL,
        "program_root": str(root),
        "data_root": str(data),
    }
    if managed_by:
        record["managed_by"] = managed_by
    return SimpleNamespace(
        root=root,
        managed_by=managed_by,
        service=SimpleNamespace(
            home=tmp_path,
            system=sys.platform,
            selection=tmp_path / "selection.json",
            recorded=lambda: record.copy(),
            login_service_exists=lambda: False,
            path=tmp_path / "service",
        ),
        source_checkout_ownership=lambda *_: pytest.fail(
            "source ownership must not run before recovery"),
        preflight_stop=lambda *_: pytest.fail(
            "mutable workspace preflight must not run before recovery"),
        existing=lambda *_: None,
    )


def _prepare_cli(monkeypatch, tmp_path, root, data, backend):
    root.mkdir(parents=True)
    data.mkdir(parents=True)
    write_json(root / "install.json", {
        "owner": LABEL,
        "data_root": str(data),
        **({"managed_by": "pinokio"} if backend.managed_by == "pinokio" else {}),
    })
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(platform, "private_directory", lambda path: Path(path).mkdir(
        parents=True, exist_ok=True))
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: backend)


@pytest.mark.parametrize("missing", ["manifest", "uv"])
def test_regular_cli_recovers_pending_migration_before_preflight_or_fresh_inputs(
        tmp_path, monkeypatch, missing):
    root, data = tmp_path / "program", tmp_path / "workspace"
    backend = _cli_backend(tmp_path, root, data)
    _prepare_cli(monkeypatch, tmp_path, root, data, backend)
    write_json(root / "update-transaction.json", {
        "format": "astrid-install-update/v1",
        "operation_id": "a" * 32,
        "transaction_kind": "migration",
        "step": "restoring_realm",
        "data_root": str(data),
    })
    recovered = []

    def recover(installer):
        recovered.append(read_json(root / "update-transaction.json")["step"])
        # Simulate recovery restoring the realm that was absent at the crash.
        restored = data / "runtime" / "realms" / "original-realm"
        restored.mkdir(parents=True)
        write_json(root / "update-transaction.json", {
            "step": "rolled_back", "outcome": "recovered",
        })

    monkeypatch.setattr(cli.Installer, "_recover_update", recover)
    manifest = tmp_path / "missing-manifest.json"
    uv = tmp_path / "missing-uv"
    if missing == "manifest":
        monkeypatch.setattr(cli, "download", lambda *_args: pytest.fail(
            "the CLI should return after pending recovery"))
        args = ["--update", "--root", str(root), "--manifest", str(manifest),
                "--uv", str(tmp_path / "valid-uv")]
    else:
        manifest.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(cli, "validate_manifest", lambda _value: {})
        args = ["--update", "--root", str(root), "--manifest", str(manifest), "--uv", str(uv)]

    assert cli.main(args) == 0
    assert recovered == ["restoring_realm"]
    assert (data / "runtime" / "realms" / "original-realm").is_dir()
    assert read_json(root / "update-transaction.json")["step"] == "rolled_back"


def test_regular_cli_recovers_app_journal_without_holding_installer_lock(
        tmp_path, monkeypatch):
    root, data = tmp_path / "app" / "astrid", tmp_path / "workspace"
    backend = _cli_backend(tmp_path, root, data, managed_by="pinokio")
    _prepare_cli(monkeypatch, tmp_path, root, data, backend)
    write_json(root / "update-transaction.json", {
        "transaction_kind": "app", "step": "stopped", "data_root": str(data),
    })
    recovery = []

    def recover_app(actual_root, actual_backend):
        assert actual_root == root
        assert actual_backend is backend
        path = app_update.installer_lock_path(backend.service.home, backend.service.system)
        with path.open("a+b") as stream:
            platform.lock_file(stream.fileno(), blocking=False)
            platform.unlock_file(stream.fileno())
        recovery.append("called")

    monkeypatch.setattr(app_update, "recover_app", recover_app)
    assert cli.main(["--root", str(root), "--update", "--uv", str(tmp_path / "absent-uv"),
                     "--manifest", str(tmp_path / "absent-manifest")]) == 0
    assert recovery == ["called"]


def test_update_app_cli_recovers_before_checking_manifest_or_uv(tmp_path, monkeypatch):
    root, data = tmp_path / "app" / "astrid", tmp_path / "workspace"
    backend = _cli_backend(tmp_path, root, data, managed_by="pinokio")
    _prepare_cli(monkeypatch, tmp_path, root, data, backend)
    write_json(root / "update-transaction.json", {
        "transaction_kind": "app", "step": "stopped", "data_root": str(data),
    })
    monkeypatch.setattr(app_update, "selection", lambda *_args, **_kwargs: (None, data, {}))
    recovered = []
    monkeypatch.setattr(app_update, "recover_app", lambda *_args, **_kwargs: recovered.append(True))
    monkeypatch.setattr(cli, "download", lambda *_args: pytest.fail(
        "recovery must not download a manifest"))
    assert cli.main(["--update-app", "--managed-by", "pinokio", "--root", str(root),
                     "--uv", str(tmp_path / "absent-uv")]) == 0
    assert recovered == [True]


def test_public_cli_restores_removed_realm_before_mutable_checks_or_update_inputs(
        tmp_path, monkeypatch):
    root, data = tmp_path / "program", tmp_path / "workspace"
    root.mkdir()
    old_root = root / "versions" / "old"
    old_root.mkdir(parents=True)
    profile = old_root / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    old = Version(old_root, Path(sys.executable), profile, {}, release=7)
    write_json(old_root / "version.json", old.document())
    switch(root, old)
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})

    realm_root = data / "runtime" / "realms" / "original-realm"
    realm_root.mkdir(parents=True)
    (realm_root / "workspace.txt").write_text("before", encoding="utf-8")
    (data / "runtime" / "catalog.json").write_text("current-but-corrupt", encoding="utf-8")
    backup = root / "backups" / "retained"
    (backup / "realm").mkdir(parents=True)
    (backup / "support" / "runtime" / "realms" / "original-realm").mkdir(parents=True)
    (backup / "realm" / "workspace.txt").write_text("original bytes", encoding="utf-8")
    write_json(backup / "support" / "runtime" / "catalog.json", {"selected_realm_id": "original-realm"})
    write_json(backup / "support" / "runtime" / "realms" / "original-realm" / "realm.json",
               {"realm_id": "original-realm"})

    # Model the crash between the two restore renames: the selected realm no
    # longer exists, while its retained backup and journal are durable.
    realm_root.rename(realm_root.with_name("original-realm.failed-before-restore"))
    write_json(root / "update-transaction.json", {
        "format": "astrid-install-update/v1",
        "operation_id": "c" * 32,
        "transaction_kind": "migration",
        "step": "recovering",
        "data_root": str(data),
        "old": old.document(),
        "candidate": old.document(),
        "realm": {"realm_id": "original-realm", "realm_root": str(realm_root)},
        "backup": {"root": str(backup), "kind": "clone"},
    })

    backend = Backend(root, tmp_path / "install.log", tmp_path / "missing-uv")
    backend.managed_by = None
    backend.stop = lambda *_args, **_kwargs: None
    backend.register = lambda *_args, **_kwargs: None
    backend.workspace = lambda *_args, **_kwargs: {"realm_id": "original-realm"}
    backend.service = SimpleNamespace(
        home=tmp_path, system=sys.platform, selection=tmp_path / "selection.json",
        recorded=lambda: {"owner": LABEL, "data_root": str(data)},
    )
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(platform, "private_directory", lambda path: Path(path).mkdir(
        parents=True, exist_ok=True))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setattr(Installer, "_activate", lambda *_args, **_kwargs: None)

    assert cli.main(["--root", str(root), "--update",
                     "--manifest", str(tmp_path / "unavailable-manifest"),
                     "--uv", str(tmp_path / "unavailable-uv")]) == 0
    assert current(root) == old
    assert (realm_root / "workspace.txt").read_text(encoding="utf-8") == "original bytes"
    assert read_json(data / "runtime" / "catalog.json")["selected_realm_id"] == "original-realm"
    assert read_json(root / "update-transaction.json")["step"] == "rolled_back"
