"""The public installer recovers durable updates before consulting fresh inputs."""

import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from runtime_protocol import platform

from astrid.installer import app_update, cli, update_job
from astrid.installer.backend import Backend
from astrid.installer.core import (
    Installer,
    VerificationError,
    Version,
    current,
    read_json,
    switch,
    write_json,
)
from astrid.installer.service import LABEL, SERVICE_SETTINGS, Service, command


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


@pytest.mark.parametrize("update_flag", [False, True], ids=["install", "update"])
def test_recovery_only_cli_sweeps_owned_job_under_installer_lock_after_recovery(
        tmp_path, monkeypatch, update_flag):
    root, data = tmp_path / "program", tmp_path / "workspace"
    backend = _cli_backend(tmp_path, root, data)
    _prepare_cli(monkeypatch, tmp_path, root, data, backend)
    operation_id = "a" * 32

    def descriptor(operation, *, name=None):
        expected = f"astrid-update-{operation}.service"
        return {
            "format": "astrid-update-platform/v1", "system": "linux",
            "name": name or expected, "unit": name or expected,
            "operation_id": operation, "user": "123",
        }

    inactive = descriptor(operation_id)
    active = descriptor("b" * 32)
    unqueryable = descriptor("c" * 32)
    unrelated = descriptor("d" * 32, name="foreign.service")
    marker = read_json(root / "install.json")
    marker["update_job"] = {
        "format": update_job.JOB_FORMAT, "operation_id": operation_id,
        "state": "active", "job_name": f"astrid-update-{operation_id}",
        "platform": inactive, "cleanup_status": "pending",
        "message": "Update is running.",
    }
    original_residues = [
        {"operation_id": "b" * 32, "descriptor": active,
         "cleanup_status": "residue", "residue": active["name"]},
        {"operation_id": "c" * 32, "descriptor": unqueryable,
         "cleanup_status": "unknown", "cleanup_error": "manager query unavailable",
         "residue": unqueryable["name"]},
        {"operation_id": "d" * 32, "descriptor": unrelated,
         "cleanup_status": "unknown", "residue": unrelated["name"]},
    ]
    marker["update_job_residue"] = original_residues
    write_json(root / "install.json", marker)
    write_json(root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "e" * 32,
        "transaction_kind": "migration", "step": "stopping", "data_root": str(data),
        "update_job": {"operation_id": operation_id},
    })
    lock_path = cli.installer_lock_path(Path.home(), sys.platform)
    events = []

    def assert_installer_lock_held():
        with lock_path.open("a+b") as competing:
            with pytest.raises(BlockingIOError):
                platform.lock_file(competing.fileno(), blocking=False)

    def recover(installer):
        assert isinstance(installer, Installer)
        assert_installer_lock_held()
        events.append("recover")
        write_json(root / "update-transaction.json", {
            "format": "astrid-install-update/v1", "operation_id": "e" * 32,
            "transaction_kind": "migration", "step": "rolled_back", "data_root": str(data),
            "update_job": {"operation_id": operation_id},
        })

    class Adapter:
        def is_active(self, value):
            assert_installer_lock_held()
            assert read_json(root / "update-transaction.json")["step"] == "rolled_back"
            events.append(("query", value["name"]))
            if value == inactive:
                return False
            if value == active:
                return True
            if value == unqueryable:
                raise RuntimeError("manager query unavailable")
            pytest.fail(f"unexpected platform query: {value}")

        def cleanup(self, value, **_kwargs):
            assert_installer_lock_held()
            assert read_json(root / "update-transaction.json")["step"] == "rolled_back"
            events.append(("cleanup", value["name"]))

    monkeypatch.setattr(cli.Installer, "_recover_update", recover)
    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    monkeypatch.setattr(cli, "download", lambda *_args: pytest.fail(
        "recovery-only CLI must return without fetching new inputs"))

    args = ["--root", str(root), "--manifest", str(tmp_path / "missing-manifest"),
            "--uv", str(tmp_path / "missing-uv")]
    if update_flag:
        args.insert(0, "--update")
    assert cli.main(args) == 0

    assert events == [
        "recover",
        ("query", inactive["name"]),
        ("cleanup", inactive["name"]),
        ("query", inactive["name"]),
        ("query", active["name"]),
        ("query", unqueryable["name"]),
    ]
    repaired = read_json(root / "install.json")
    assert repaired["update_job"]["state"] == "recovery-needed"
    assert repaired["update_job"]["cleanup_status"] == "clean"
    assert repaired["update_job_residue"] == original_residues


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
    write_json(backup / "support" / "runtime" / "catalog.json", {
        "version": 1,
        "selected_realm_id": "original-realm",
        "realms": [{"realm_id": "original-realm", "display_name": "Original workspace",
                    "data_root": str(realm_root)}],
        "source_profiles": {},
    })
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
    backend.publish_launch_admission = lambda *_args: None
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



# Frozen historical wrapper for retained-task compatibility; never used by production.
def _pre_x1b_command(version: Version, data: Path, *, windows: bool = False, log: Path | None = None,
            env_file: Path | None = None, extra_args: list[str] | None = None,
            lock_fd: int | None = None, installer_operation_id: str | None = None) -> list[str]:
    python = version.python.with_name("pythonw.exe") if windows else version.python
    arguments = ["run", "--profile", "astrid", "--data-root", str(data),
                 "--source-manifest", str(version.profile), *(extra_args or [])]
    if (env_file is None and not windows and lock_fd is None and not installer_operation_id
            and "ASTRID_INSTALLER_OPERATION_ID" not in os.environ
            and "ASTRID_INSTALLER_OPERATION_ID" not in version.env):
        return [str(python), "-X", "utf8", "-I", "-m", "astrid_runtime", *arguments]
    # Task Scheduler cannot set an environment and Hidden does not hide a
    # console. pythonw executes the foreground Runtime in this same process.
    script = "import json,os,sys\nfrom pathlib import Path\n"
    script += f"os.environ.update({version.env!r})\n"
    if installer_operation_id:
        transaction_path = version.root.parent.parent / "update-transaction.json"
        script += f"_astrid_operation_id={installer_operation_id!r}\n"
        script += f"_astrid_version={version.document()!r}\n"
        script += f"_astrid_transaction_path=Path({str(transaction_path)!r})\n"
        script += (
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
    if lock_fd is not None:
        # The foreground owner inherited this locked descriptor across exec.
        # Keep it open here, but never pass it to a subsequently exec'd child.
        script += f"os.set_inheritable({lock_fd},False)\n"
    if windows:
        if log is None:
            raise ValueError("A log path is required for the Windows Runtime service.")
        log = log.with_name("runtime-service.log")
        script += f"sys.stdout=sys.stderr=open({str(log)!r},'a',encoding='utf-8',buffering=1)\n"
    if env_file is not None:
        script += (f"path=Path({str(env_file)!r})\n"
                   "settings=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}\n"
                   f"if (not isinstance(settings,dict) or set(settings)-set({sorted(SERVICE_SETTINGS)!r}) "
                   "or any(not isinstance(value,str) for value in settings.values())):\n"
                   "    raise ValueError('Invalid Astrid service-env.json; use the documented string settings.')\n"
                   "os.environ.update(settings)\n")
    script += f"from astrid_runtime.cli import main\nsys.exit(main({arguments!r}))"
    return [str(python), "-X", "utf8", "-I", "-c", script]


def _interrupted_bound_task_restore(tmp_path, monkeypatch):
    """Drive the real rollback transaction into the legacy catalog-gap shape."""
    import importlib.util

    fixture_path = Path(__file__).with_name("test_installer_transaction_authority.py")
    spec = importlib.util.spec_from_file_location("bound_task_restore_fixture", fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    (backend, data, paths, realm, boundary, manager_calls, old, candidate,
     backup_root, journal_path) = fixture._readable_restore_gap(tmp_path, monkeypatch)

    (realm / "workspace-state.txt").write_text("candidate realm", encoding="utf-8")
    shutil.copytree(realm, backup_root / "realm")
    (backup_root / "realm" / "workspace-state.txt").write_text(
        "old realm", encoding="utf-8")

    # Run the real transaction driver through candidate activation failure.
    # The simulated pre-amendment restore then replaces the realm, deletes its
    # catalog, and is interrupted; the public retry uses today's real restore.
    shutil.copy2(backup_root / "support" / "runtime" / "catalog.json", paths.catalog_path)
    switch(backend.root, old)
    stop_versions = []
    backend.stop = lambda version, _data: stop_versions.append(version)

    class RestoreInterrupted(BaseException):
        pass

    def interrupted_restore(_old, _data, _realm, _backup):
        (realm / "workspace-state.txt").write_text("old realm", encoding="utf-8")
        paths.catalog_path.unlink()
        raise RestoreInterrupted()

    backend.restore = interrupted_restore
    installer = Installer(backend.root, data, backend, say=lambda _line: None)

    def fail_candidate_activation(version, _data, _realm):
        raise VerificationError("candidate activation failed")

    try:
        installer._run_update_transaction(
            old, read_json(journal_path)["realm"],
            prepare=lambda: candidate,
            stop=backend.stop,
            switch_candidate=lambda version: switch(backend.root, version),
            switch_old=lambda version, _candidate: switch(backend.root, version),
            activate=fail_candidate_activation,
            activate_old=lambda *_args: None,
            backup=lambda *_args: {"root": str(backup_root), "kind": "clone"},
            migrate=lambda *_args: None,
        )
    except RestoreInterrupted:
        pass
    else:
        pytest.fail("the simulated restore interruption did not occur")

    transaction = read_json(journal_path)
    assert stop_versions == [old, candidate]  # rollback retained the candidate launch context
    assert transaction["step"] == "recovering"
    transaction["launch_intent"] = {
        "operation_id": transaction["operation_id"],
        "purpose": "service_start",
        "version": candidate.document(),
    }
    write_json(journal_path, transaction)  # Retained pre-X1b extra; grants no stop authority.
    assert not paths.catalog_path.exists()
    # Model a subsequent selected pointer to the old version. Recovery must
    # still validate the retained candidate action using launch_intent.
    switch(backend.root, old)
    backend.stop = types.MethodType(Backend.stop, backend)
    backend.restore = types.MethodType(Backend.restore, backend)
    return (fixture, backend, data, paths, realm, boundary, manager_calls, old,
            candidate, backup_root, journal_path, transaction["operation_id"])


@pytest.mark.parametrize("mutation", ["early", "callback", "operation_id"])
def test_public_cli_recovers_bound_windows_task_after_catalog_gap_refuses_mutation(
        tmp_path, monkeypatch, mutation):
    """A bound action stays valid through both real recovery validations only."""
    (fixture, backend, data, paths, realm, boundary, manager_calls, old,
     candidate, backup_root, journal_path, operation_id) = _interrupted_bound_task_restore(
        tmp_path, monkeypatch)

    backend.service.system = "win32"
    backend.service.validate_stop_target = types.MethodType(
        Service.validate_stop_target, backend.service)
    action_operation_id = "b" * 32 if mutation == "operation_id" else operation_id
    launch_version = Version.load(read_json(journal_path)["launch_intent"]["version"])
    argv = _pre_x1b_command(launch_version, data, windows=True, log=backend.log,
                   env_file=backend.service.env_file,
                   installer_operation_id=action_operation_id)
    action = [{"Execute": argv[0],
               "Arguments": subprocess.list2cmdline(argv[1:])}]
    changed_action = [{**action[0], "Arguments": action[0]["Arguments"] + " --changed"}]
    query_count = 0

    canonical_argv = command(old, data, windows=True, log=backend.log, env_file=backend.service.env_file)
    canonical = [{"Execute": canonical_argv[0], "Arguments": subprocess.list2cmdline(canonical_argv[1:])}]
    stored_action = action

    def query_task(script):
        nonlocal query_count, stored_action
        if "Set-ScheduledTask" in script:
            stored_action = canonical
            return ""
        query_count += 1
        if mutation == "early":
            result = changed_action
        elif mutation == "operation_id":
            result = action
        elif mutation == "callback" and query_count == 3:
            result = [{**canonical[0], "Arguments": canonical[0]["Arguments"] + " --changed"}]
        else:
            result = stored_action
        return json.dumps(result)

    backend.service.powershell = query_task
    restore_calls = []
    real_restore = backend.restore

    def restore(*args):
        restore_calls.append(args)
        return real_restore(*args)

    backend.restore = restore
    backend.register = lambda *_args, **_kwargs: None
    backend.start = lambda *_args, **_kwargs: None
    backend.verify = lambda *_args, **_kwargs: None
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(platform, "private_directory", lambda path: Path(path).mkdir(
        parents=True, exist_ok=True))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))

    journal_before = journal_path.read_bytes()
    backup_before = fixture._backup_bytes(backup_root)
    pointer_before = (backend.root / "current").read_bytes()
    selection_before = backend.service.selection.read_bytes()
    import sqlite3

    with sqlite3.connect(f"file:{realm / 'realm.sqlite3'}?mode=ro", uri=True) as connection:
        realm_db_before = tuple(connection.iterdump())
    result = cli.main(["--update", "--root", str(backend.root),
                       "--uv", str(tmp_path / "unused-uv"),
                       "--manifest", str(tmp_path / "unused-manifest")])

    assert result == 1
    assert query_count == (3 if mutation == "callback" else 1)
    assert journal_path.read_bytes() == journal_before
    assert fixture._backup_bytes(backup_root) == backup_before
    assert not paths.catalog_path.exists()
    assert backend._restore_catalog_writes == []
    assert restore_calls == []
    assert boundary.stop_calls == 0
    assert manager_calls == []
    assert (realm / "workspace-state.txt").read_text(encoding="utf-8") == "old realm"
    assert (backend.root / "current").read_bytes() == pointer_before
    assert backend.service.selection.read_bytes() == selection_before
    with sqlite3.connect(f"file:{realm / 'realm.sqlite3'}?mode=ro", uri=True) as connection:
        assert tuple(connection.iterdump()) == realm_db_before
    backend.service.disable.assert_not_called()
    backend.service.stop.assert_not_called()


def test_public_cli_recovers_bound_windows_task_after_candidate_activation_failure(
        tmp_path, monkeypatch):
    """The retained exact production action reaches C1, catalog repair, and replay."""
    (fixture, backend, data, paths, realm, boundary, manager_calls, old,
     candidate, backup_root, journal_path, operation_id) = _interrupted_bound_task_restore(
        tmp_path, monkeypatch)

    backend.service.system = "win32"
    backend.service.validate_stop_target = types.MethodType(
        Service.validate_stop_target, backend.service)
    launch_version = Version.load(read_json(journal_path)["launch_intent"]["version"])
    argv = _pre_x1b_command(launch_version, data, windows=True, log=backend.log,
                   env_file=backend.service.env_file,
                   installer_operation_id=operation_id)
    action = [{"Execute": argv[0],
               "Arguments": subprocess.list2cmdline(argv[1:])}]
    task_queries = []
    stored_action = action
    canonical_argv = command(old, data, windows=True, log=backend.log, env_file=backend.service.env_file)
    canonical = [{"Execute": canonical_argv[0], "Arguments": subprocess.list2cmdline(canonical_argv[1:])}]

    def query_task(script):
        nonlocal stored_action
        task_queries.append(script)
        if "Set-ScheduledTask" in script:
            stored_action = canonical
            return ""
        return json.dumps(stored_action)

    backend.service.powershell = query_task
    restore_calls = []
    real_restore = backend.restore
    backup_before = fixture._backup_bytes(backup_root)

    def restore(*args):
        assert paths.catalog_path.is_file()  # C1's atomic catalog repair completed first
        restore_calls.append(args)
        return real_restore(*args)

    backend.restore = restore
    backend.register = lambda *_args, **_kwargs: None
    backend.start = lambda *_args, **_kwargs: None
    backend.verify = lambda *_args, **_kwargs: None
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(platform, "private_directory", lambda path: Path(path).mkdir(
        parents=True, exist_ok=True))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))

    assert cli.main(["--update", "--root", str(backend.root),
                     "--uv", str(tmp_path / "unused-uv"),
                     "--manifest", str(tmp_path / "unused-manifest")]) == 0
    assert len(task_queries) == 4  # legacy read, action migration, early and fenced revalidation
    assert boundary.stop_calls == 0  # no owner publication; C1 admits the idle readable realm
    assert manager_calls == []
    backend.service.disable.assert_called_once()
    backend.service.stop.assert_called_once()
    assert len(backend._restore_catalog_writes) == 2  # C1 repair, then atomic restore replacement
    assert paths.catalog_path.is_file()
    assert read_json(paths.catalog_path)["selected_realm_id"] == "realm-1"
    assert len(restore_calls) == 1
    assert (realm / "workspace-state.txt").read_text(encoding="utf-8") == "old realm"
    assert fixture._backup_bytes(backup_root) == backup_before
    assert current(backend.root) == old
    assert read_json(journal_path)["step"] == "rolled_back"
