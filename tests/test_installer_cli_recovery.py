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
    old_root = root / "versions" / "old"
    old_root.mkdir(parents=True)
    python, profile = old_root / "python", old_root / "profile.json"
    python.write_text("placeholder", encoding="utf-8")
    profile.write_text("{}", encoding="utf-8")
    old = Version(old_root, python, profile, {}, release=4, managed_by="pinokio")
    write_json(old_root / "version.json", old.document())
    switch(root, old)
    write_json(root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "a" * 32,
        "transaction_kind": "app", "step": "stopped", "data_root": str(data),
        "old": old.document(), "candidate": old.document(),
        "realm": {"realm_id": "realm", "realm_root": str(data / "realm")}, "backup": None,
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


@pytest.mark.parametrize("step", ["staged", "stopping", "stopped", "migrated", "switched", "verified"])
def test_pinokio_cli_recovers_migration_boundary_before_retrying_new_inputs(
        tmp_path, monkeypatch, step):
    root, data = tmp_path / "app" / "astrid", tmp_path / "workspace"
    backend = _cli_backend(tmp_path, root, data, managed_by="pinokio")
    _prepare_cli(monkeypatch, tmp_path, root, data, backend)
    old_root, candidate_root = root / "versions" / "old", root / "versions" / "candidate"

    def version(folder, release):
        folder.mkdir(parents=True)
        python, profile = folder / "python", folder / "profile.json"
        python.write_text("placeholder", encoding="utf-8")
        profile.write_text("{}", encoding="utf-8")
        value = Version(folder, python, profile, {}, release=release, managed_by="pinokio")
        write_json(folder / "version.json", value.document())
        return value

    old, candidate = version(old_root, 4), version(candidate_root, 5)
    selected = candidate if step in {"switched", "verified"} else old
    switch(root, selected)
    original_workspace = data / "workspace.json"
    original_workspace.write_text("old workspace", encoding="utf-8")
    backup_root = root / "backups" / "retained"
    backup_root.mkdir(parents=True)
    backup = {"root": str(backup_root)} if step in {"migrated", "switched", "verified"} else None
    write_json(root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "9" * 32,
        "transaction_kind": "migration", "step": step, "data_root": str(data),
        "old": old.document(), "candidate": candidate.document(),
        "realm": {"realm_id": "realm", "realm_root": str(data / "realm")},
        "backup": backup,
    })
    events = []

    def stop(version, _data):
        events.append(("stop", version.root.name))

    def start(version, _data, realm_id):
        events.append(("ready", version.root.name, realm_id))

    def restore(_old, _data, _realm, _backup):
        events.append(("restore",))
        original_workspace.write_text("old workspace", encoding="utf-8")

    backend.restore = restore
    backend.persist_identity = lambda version: events.append(("identity", version.root.name))
    manifest = tmp_path / "manifest.json"
    uv = tmp_path / "uv"
    write_json(manifest, {"release": 6})
    uv.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(cli, "validate_manifest", lambda value: value)
    monkeypatch.setattr(cli, "download", lambda *_args: pytest.fail(
        "recovery must return before fetching new update inputs"))
    monkeypatch.setattr(app_update, "PinokioLifecycle", lambda _root, _backend: SimpleNamespace(
        validate=lambda _version: None, stop=stop, start=start))

    first = ["--root", str(root), "--update", "--manifest", str(manifest), "--uv", str(uv)]
    assert cli.main(first) == 0
    journal = read_json(root / "update-transaction.json")
    assert journal["step"] == ("complete" if step == "staged" else "rolled_back")
    assert current(root) == old
    assert ("restore",) in events if backup else ("restore",) not in events
    if step == "staged":
        assert events == []
    else:
        assert ("ready", "old", "realm") in events
        assert events[0][0] == "stop"
        assert not original_workspace.read_text(encoding="utf-8") == "candidate workspace"

    # Retry is a separate invocation: only now may new manifest and staging
    # proceed. This keeps recovery ahead of C2b release dispatch on every retry.
    routed = []
    backend.preflight_stop = lambda _data: routed.append("preflight")
    monkeypatch.setattr(app_update, "update_pinokio", lambda *args, **kwargs: routed.append(
        (args[0], kwargs.get("force"))))
    assert cli.main(first) == 0
    assert routed == ["preflight", (root, True)]


@pytest.mark.parametrize("source", ["explicit", "hosted"])
def test_installed_cli_validates_candidate_without_requiring_staging_uv(
        tmp_path, monkeypatch, source):
    root, data = tmp_path / "program", tmp_path / "workspace"
    backend = _cli_backend(tmp_path, root, data)
    _prepare_cli(monkeypatch, tmp_path, root, data, backend)
    old_root = root / "versions" / "old"
    old_root.mkdir(parents=True)
    profile = old_root / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    old = Version(old_root, Path(sys.executable), profile, {}, release=5)
    write_json(old_root / "version.json", old.document())
    switch(root, old)
    backend.source_checkout_ownership = lambda *_: False
    backend.preflight_stop = lambda *_: None

    candidate = {"release": 6, "candidate": source}
    manifest_path = tmp_path / "candidate.json"
    if source == "explicit":
        write_json(manifest_path, candidate)
    else:
        def download(_url, destination):
            write_json(destination, candidate)

        monkeypatch.setattr(cli, "download", download)

    validated = []
    monkeypatch.setattr(cli, "validate_manifest", lambda value: validated.append(value) or value)
    dispatched = []

    class CapturingInstaller:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, mode, manifest, **_kwargs):
            dispatched.append((mode, manifest))

    monkeypatch.setattr(cli, "Installer", CapturingInstaller)
    args = ["--root", str(root), "--uv", str(tmp_path / "missing-uv")]
    if source == "explicit":
        args.extend(["--manifest", str(manifest_path)])

    assert cli.main(args) == 0
    assert validated == [candidate]
    assert dispatched == [("install", candidate)]


def test_update_flag_is_hidden_but_remains_mutually_exclusive():
    help_text = cli.parser().format_help()
    assert "--update]" not in help_text
    assert "--update " not in help_text
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["--update", "--uninstall"])


def _valid_cli_manifest(release):
    return {
        "schema_version": 1,
        "release": release,
        "agent": {"sha": "a" * 40},
        "runtime": {"sha": "b" * 40},
        "tools": {
            "uv": {"version": "0.11.32"},
            "python": {"version": "3.11.13"},
            "bun": {"version": "1.3.14"},
            "node": {"version": "20.19.4"},
            "ffmpeg": {"version": "7.0.2"},
            "omp": {"source": "@oh-my-pi/pi-coding-agent@18.12.6"},
        },
    }


def _prepare_gateway_lifecycle(tmp_path, monkeypatch, *, installed=9):
    from tests.test_installer_lifecycle import LifecycleBackend

    root, data = tmp_path / "program", tmp_path / "workspace"
    root.mkdir()
    data.mkdir()
    old_root = root / "versions" / "old"
    old_root.mkdir(parents=True)
    profile = old_root / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    old = Version(old_root, Path(sys.executable), profile, {}, release=installed)
    write_json(old_root / "version.json", old.document())
    switch(root, old)
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    record = {"owner": LABEL, "program_root": str(root), "data_root": str(data)}
    backend = LifecycleBackend(root, data)
    backend.old = old
    backend.service = SimpleNamespace(recorded=lambda: record.copy())
    backend.managed_by = None
    backend.source_checkout_ownership = lambda *_args: False
    backend.restart_required = lambda: False
    backend.verify = lambda *_args, **_kwargs: backend.calls.append(("verify",))
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(platform, "private_directory", lambda path: Path(path).mkdir(
        parents=True, exist_ok=True))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setattr("astrid.installer.core.install_launcher", lambda *_args: None)
    return root, data, old, backend


@pytest.mark.parametrize(
    ("candidate", "forced", "unhealthy", "expected_stage"),
    [
        (10, False, False, True),
        (9, False, False, False),
        (8, False, False, False),
        (9, True, False, True),
        (9, False, True, False),
    ],
    ids=["newer", "same", "older", "forced", "unhealthy-same"],
)
@pytest.mark.parametrize("manifest_source", ["explicit", "hosted"])
def test_gateway_dispatches_installed_release_through_real_installer(
        tmp_path, monkeypatch, candidate, forced, unhealthy, expected_stage, manifest_source, capsys):
    from astrid.core.gateway import main as gateway_main

    root, data, old, backend = _prepare_gateway_lifecycle(tmp_path, monkeypatch)
    if unhealthy:
        attempts = []

        def verify(*_args, **_kwargs):
            attempts.append(True)
            backend.calls.append(("verify",))
            if len(attempts) == 1:
                raise cli.VerificationError("not healthy")

        backend.verify = verify

    manifest = _valid_cli_manifest(candidate)
    manifest_path = tmp_path / "manifest.json"
    if manifest_source == "explicit":
        write_json(manifest_path, manifest)
    else:
        monkeypatch.setattr(cli, "download", lambda _url, path: write_json(path, manifest))
    args = ["install", "--root", str(root), "--uv", str(tmp_path / "absent-uv")]
    if manifest_source == "explicit":
        args.extend(["--manifest", str(manifest_path)])
    if forced:
        args.append("--update")

    refused_older = candidate < old.release and not forced
    assert gateway_main(args) == (1 if refused_older else 0)
    if refused_older:
        output = capsys.readouterr().out
        assert "This Astrid is newer than astrid.haus offers." in output
        assert "Try again when astrid.haus has a newer release." in output
    names = [call[0] for call in backend.calls]
    assert ("stage" in names) is expected_stage
    if expected_stage:
        assert names.index("stage") < names.index("stop")
        assert current(root).release == candidate
    else:
        assert current(root) == old
        assert not {"stage", "backup", "upgrade"}.intersection(names)
        assert ("stop" in names) is unhealthy
    if unhealthy:
        assert names.count("verify") == 2
        assert "start" in names


@pytest.mark.parametrize("failure", ["fetch", "parse", "release"])
def test_gateway_candidate_failures_preserve_installed_pointer_and_workspace(
        tmp_path, monkeypatch, failure):
    from astrid.core.gateway import main as gateway_main

    root, data, old, backend = _prepare_gateway_lifecycle(tmp_path, monkeypatch)
    sentinel = data / "workspace-data.txt"
    sentinel.write_text("retain", encoding="utf-8")
    pointer = (root / "current").read_bytes()
    args = ["install", "--root", str(root), "--uv", str(tmp_path / "absent-uv")]
    if failure == "fetch":
        monkeypatch.setattr(cli, "download", lambda *_args: (_ for _ in ()).throw(OSError("offline")))
    else:
        manifest_path = tmp_path / "manifest.json"
        if failure == "parse":
            manifest_path.write_text("{", encoding="utf-8")
        else:
            manifest = _valid_cli_manifest("bad")
            write_json(manifest_path, manifest)
        args.extend(["--manifest", str(manifest_path)])

    assert gateway_main(args) == 1
    assert (root / "current").read_bytes() == pointer
    assert current(root) == old
    assert sentinel.read_text(encoding="utf-8") == "retain"
    assert not {"stage", "stop", "backup", "upgrade"}.intersection(call[0] for call in backend.calls)


def test_recovery_only_return_then_next_gateway_call_compares_restored_release(
        tmp_path, monkeypatch):
    from astrid.core.gateway import main as gateway_main

    root, data, _old, backend = _prepare_gateway_lifecycle(tmp_path, monkeypatch, installed=99)
    restored_root = root / "versions" / "restored"
    restored_root.mkdir(parents=True)
    profile = restored_root / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    restored = Version(restored_root, Path(sys.executable), profile, {}, release=7)
    write_json(restored_root / "version.json", restored.document())
    write_json(root / "update-transaction.json", {
        "transaction_kind": "migration", "step": "stopping", "data_root": str(data),
    })
    recovery = []

    def recover(installer):
        recovery.append(True)
        switch(root, restored)
        write_json(root / "update-transaction.json", {"step": "rolled_back"})

    monkeypatch.setattr(Installer, "_recover_update", recover)
    assert gateway_main(["install", "--root", str(root), "--manifest", str(tmp_path / "missing"),
                         "--uv", str(tmp_path / "missing-uv")]) == 0
    assert recovery == [True]
    assert not backend.calls

    manifest_path = tmp_path / "next-manifest.json"
    write_json(manifest_path, _valid_cli_manifest(8))
    assert gateway_main(["install", "--root", str(root), "--manifest", str(manifest_path),
                         "--uv", str(tmp_path / "missing-uv")]) == 0
    assert any(call[0] == "stage" for call in backend.calls)
    assert current(root).release == 8


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
