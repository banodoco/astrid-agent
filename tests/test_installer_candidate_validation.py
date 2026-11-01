"""Candidate admission reads the existing workspace before any lifecycle changes."""

import contextlib
import io
import json
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrid.installer import app_update, core, tools
from astrid.installer.backend import Backend
from astrid.installer.core import Version, write_json
from astrid.installer.service import LABEL


@pytest.fixture
def candidate(tmp_path):
    root, data = tmp_path / "program", tmp_path / "data"
    folder = root / "versions" / "candidate"
    python = folder / "py" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    for name in ("r", "a", "py"):
        (folder / name).mkdir(exist_ok=True)
    profile = folder / "profile.json"
    write_json(folder / "services.json", {"version": 1, "services": [{
        "name": "pack-host", "argv": ["/usr/bin/python", "-c", "pass"], "restart": "always",
    }]})
    write_json(profile, {
        "profile": "astrid",
        "runtime_checkout": str(folder / "r"), "source_checkout": str(folder / "a"),
        "runtime_environment": str(folder / "py"), "service_declaration": str(folder / "services.json"),
    })
    version = Version(folder, python, profile, {})
    realm = {"realm_id": "realm-1", "realm_root": str(data / "realm-1")}
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv")
    def run_candidate_validation(argv, **_kwargs):
        # Exercise the actual matching C1 SourceProfile, startup owner, and
        # services/schema validators without a separately installed candidate venv.
        prior = sys.argv
        output = io.StringIO()
        try:
            sys.argv = ["-c", *map(str, argv[6:])]
            try:
                with contextlib.redirect_stdout(output):
                    exec(argv[5], {})
            except Exception as exc:
                raise subprocess.CalledProcessError(1, list(argv), output=str(exc)) from exc
        finally:
            sys.argv = prior
        return output.getvalue().strip()
    backend.run = run_candidate_validation
    return backend, version, data, realm


def test_candidate_admits_supported_old_schema_without_requiring_migration_first(candidate, monkeypatch):
    backend, version, data, realm = candidate
    calls = []
    monkeypatch.setattr(backend, "workspace", lambda selected, support: realm.copy())
    monkeypatch.setattr(core, "inspect_schema", lambda *args, **kwargs: calls.append((args, kwargs)))

    backend.validate_candidate(version, data, realm)

    assert calls == [((backend, version, realm), {"require_current": False})]


@pytest.mark.parametrize("changed", ["realm_id", "realm_root"])
def test_candidate_rejects_workspace_selection_change(candidate, monkeypatch, changed):
    backend, version, data, realm = candidate
    selected = {**realm, changed: "other" if changed == "realm_id" else str(data / "other")}
    monkeypatch.setattr(backend, "workspace", lambda *_args: selected)
    monkeypatch.setattr(core, "inspect_schema", lambda *_args, **_kwargs: pytest.fail("schema read after selection mismatch"))

    with pytest.raises(ValueError, match="workspace changed"):
        backend.validate_candidate(version, data, realm)


@pytest.mark.parametrize("damage", ["python", "profile", "external-source", "missing-source"])
def test_incomplete_or_escaping_candidate_is_rejected_before_workspace_access(candidate, monkeypatch, damage):
    backend, version, data, realm = candidate
    if damage == "python":
        version.python.unlink()
    elif damage == "profile":
        version.profile.unlink()
    else:
        profile = core.read_json(version.profile)
        profile["source_checkout"] = str(data) if damage == "external-source" else ""
        write_json(version.profile, profile)
    monkeypatch.setattr(backend, "workspace", lambda *_args: pytest.fail("workspace read before candidate admission"))

    with pytest.raises(ValueError, match="incomplete|outside|missing"):
        backend.validate_candidate(version, data, realm)


@pytest.mark.parametrize("damage", [
    "bad-profile", "incompatible-protocol", "incompatible-schema", "missing-source",
    "missing-environment", "missing-services", "bad-services", "missing-pack-host",
])
def test_runtime_profile_source_environment_and_services_are_admitted_before_workspace(candidate, monkeypatch, damage):
    backend, version, data, realm = candidate
    if damage == "bad-profile":
        profile = core.read_json(version.profile)
        profile["profile"] = "other"
        write_json(version.profile, profile)
    elif damage == "incompatible-protocol":
        profile = core.read_json(version.profile)
        profile["protocol_version"] = "incompatible"
        write_json(version.profile, profile)
    elif damage == "incompatible-schema":
        profile = core.read_json(version.profile)
        profile["schema_version"] = "incompatible"
        write_json(version.profile, profile)
    elif damage == "missing-source":
        (version.root / "a").rmdir()
    elif damage == "missing-environment":
        shutil.rmtree(version.root / "py")
    elif damage == "missing-services":
        (version.root / "services.json").unlink()
    elif damage == "bad-services":
        write_json(version.root / "services.json", {"not": "a declaration"})
    else:
        write_json(version.root / "services.json", {"version": 1, "services": []})
    monkeypatch.setattr(backend, "workspace", lambda *_args: pytest.fail("workspace read before candidate admission"))

    with pytest.raises(ValueError):
        backend.validate_candidate(version, data, realm)


@pytest.mark.parametrize("damage", [
    "bad-profile", "incompatible-protocol", "incompatible-schema", "missing-source",
    "missing-environment", "missing-services", "bad-services", "missing-pack-host",
])
def test_app_prepare_uses_runtime_candidate_admission_before_lifecycle_side_effects(
        candidate, monkeypatch, damage):
    backend, version, data, realm = candidate
    if damage == "bad-profile":
        profile = core.read_json(version.profile)
        profile["profile"] = "other"
        write_json(version.profile, profile)
    elif damage == "incompatible-protocol":
        profile = core.read_json(version.profile)
        profile["protocol_version"] = "incompatible"
        write_json(version.profile, profile)
    elif damage == "incompatible-schema":
        profile = core.read_json(version.profile)
        profile["schema_version"] = "incompatible"
        write_json(version.profile, profile)
    elif damage == "missing-source":
        shutil.rmtree(version.root / "a")
    elif damage == "missing-environment":
        shutil.rmtree(version.root / "py")
    elif damage == "missing-services":
        (version.root / "services.json").unlink()
    elif damage == "bad-services":
        write_json(version.root / "services.json", {"not": "a declaration"})
    else:
        write_json(version.root / "services.json", {"version": 1, "services": []})

    old_root = backend.root / "versions" / "old"
    old_python = old_root / "py" / "bin" / "python"
    old_python.parent.mkdir(parents=True)
    old_python.touch()
    old_profile = old_root / "profile.json"
    old_profile.touch()
    old = Version(old_root, old_python, old_profile, {}, managed_by="pinokio")
    write_json(old_root / "version.json", old.document())
    core.switch(backend.root, old)
    install_marker = backend.root / "install.json"
    write_json(install_marker, {"owner": LABEL, "managed_by": "pinokio",
                                "data_root": str(data), "identity": old.document()["identity"]})
    marker_before = install_marker.read_bytes()
    service_selection = backend.service.selection
    write_json(service_selection, {"owner": LABEL, "managed_by": "pinokio",
                                   "data_root": str(data), "identity": old.document()["identity"]})
    selection_before = service_selection.read_bytes()
    pointer_before = core.current(backend.root)
    workspace_marker = Path(realm["realm_root"]) / "preserve.txt"
    workspace_marker.parent.mkdir(parents=True, exist_ok=True)
    workspace_marker.write_text("keep", encoding="utf-8")
    workspace_before = workspace_marker.read_bytes()
    owner = {"running": True, "version": "old"}
    lifecycle_calls = []
    monkeypatch.setattr(backend, "workspace", lambda *_args: realm.copy())
    monkeypatch.setattr(backend, "stage", lambda *_args: version)
    monkeypatch.setattr(core, "inspect_schema", lambda *_args, **_kwargs: {"schema": 31})
    monkeypatch.setattr(app_update, "inspect_schema", lambda *_args, **_kwargs: {"schema": 31})
    monkeypatch.setattr(app_update, "selection", lambda *_args, **_kwargs: (old, data, old.document()["identity"]))
    monkeypatch.setattr(app_update.Installer, "_run_update_transaction",
                        lambda _self, _old, _realm, **kwargs: kwargs["prepare"]())

    def validate(value):
        lifecycle_calls.append("validate:" + value.root.name)

    def side_effect(name):
        def run(*_args, **_kwargs):
            lifecycle_calls.append(name)
            owner["running"] = False
        return run

    lifecycle = SimpleNamespace(
        validate=validate,
        stop=side_effect("stop"),
        start=side_effect("start"),
    )

    with pytest.raises(ValueError):
        app_update.update_app(backend.root, backend, {}, lifecycle=lifecycle, say=lambda _message: None)

    assert lifecycle_calls == ["validate:old"]
    assert owner == {"running": True, "version": "old"}
    assert core.current(backend.root) == pointer_before == old
    assert install_marker.read_bytes() == marker_before
    assert service_selection.read_bytes() == selection_before
    assert not (backend.root / "update-transaction.json").exists()
    assert workspace_marker.read_bytes() == workspace_before


def test_windows_reparse_candidate_directory_is_rejected(candidate, monkeypatch):
    backend, version, data, realm = candidate
    original = Path.lstat

    def attributes(path, *args, **kwargs):
        if path == version.root:
            return SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", attributes)
    monkeypatch.setattr(Path, "is_symlink", lambda _path: False)
    with pytest.raises(ValueError, match="cannot contain links"):
        backend.validate_candidate(version, data, realm)


def test_schema_inspection_uses_candidate_interpreter_and_preserves_windows_arguments():
    version = Version(PureWindowsPath("C:/Astrid/versions/new"),
                      PureWindowsPath("C:/Astrid/versions/new/py/Scripts/python.exe"),
                      PureWindowsPath("C:/Astrid/versions/new/profile.json"), {"PATH": "candidate"})
    realm = {"realm_root": "C:\\Astrid Data\\realm", "realm_id": "realm-1"}
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return json.dumps({"identity": {"version": 29}, "target": 31})

    value = core.inspect_schema(SimpleNamespace(run=run), version, realm, require_current=False)

    argv, kwargs = calls[0]
    assert argv[:5] == [version.python, "-X", "utf8", "-I", "-c"]
    assert argv[-2:] == [realm["realm_root"], realm["realm_id"]]
    assert kwargs == {"extra_env": version.env, "timeout": 120}
    assert value["identity"]["version"] == 29


def test_candidate_schema_inspector_is_read_only_and_accepts_migration_source(tmp_path, monkeypatch):
    from runtime_protocol.errors import ValidationError
    from runtime_protocol.store import RealmStore

    realm_root = tmp_path / "realm"
    RealmStore.initialize(realm_root, realm_id="realm-1").close()
    database = realm_root / "realm.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE timelines DROP COLUMN name")
        connection.execute("DROP TABLE user_app_state")
        connection.execute("UPDATE runtime_schema SET version=29 WHERE id=1")
    before = database.read_bytes()
    version = Version(tmp_path / "candidate", Path(sys.executable), tmp_path / "profile.json", {})
    realm = {"realm_root": str(realm_root), "realm_id": "realm-1"}

    def run(argv, **_kwargs):
        # Execute the actual candidate inspection script against matching C1
        # sources already imported on this test process's path.
        monkeypatch.setattr(sys, "argv", ["-c", *argv[-2:]])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec(argv[5], {})
        return output.getvalue()

    backend = SimpleNamespace(run=run)
    assert core.inspect_schema(backend, version, realm, require_current=False)["identity"]["version"] == 29
    with pytest.raises(ValueError, match="needs a workspace migration"):
        core.inspect_schema(backend, version, realm)
    with pytest.raises(ValidationError, match="realm identity mismatch"):
        core.inspect_schema(backend, version, {**realm, "realm_id": "other"}, require_current=False)
    assert database.read_bytes() == before


@pytest.mark.parametrize("failure", ["download", "hash", "candidate", "malformed-profile",
                                    "incompatible-profile-protocol", "incompatible-profile-schema",
                                    "missing-source", "missing-environment", "missing-service",
                                    "invalid-service", "missing-pack-host"])
def test_real_preparation_failure_leaves_old_owner_pointer_and_workspace(candidate, monkeypatch, failure):
    backend, version, data, realm = candidate
    old_root = backend.root / "versions" / "old"
    old_root.mkdir()
    old_python = old_root / "python"
    old_python.touch()
    old = Version(old_root, old_python, old_root / "profile.json", {}, release=17)
    write_json(old_root / "version.json", old.document())
    core.switch(backend.root, old)
    data.mkdir()
    (data / "user-work").write_bytes(b"original work")
    write_json(data / "owner.json", {"pid": 123, "release": 17})
    before = {path.name: path.read_bytes() for path in data.iterdir()}
    backend.service.selection = backend.root / "service-selection.json"
    write_json(backend.service.selection, {
        "owner": "haus.astrid.runtime", "program_root": str(backend.root),
        "data_root": str(data), "identity": {"release": 17, "managed_by": None},
    })
    selection_before = backend.service.selection.read_bytes()
    monkeypatch.setattr(backend, "workspace", lambda *_args: realm.copy())
    monkeypatch.setattr(backend, "has_workspace", lambda _data: True)
    for name in ("stop", "backup", "upgrade", "register"):
        monkeypatch.setattr(backend, name, Mock(side_effect=AssertionError(f"{name} before admission")))

    def response(*_args, **_kwargs):
        if failure == "download":
            raise OSError("download interrupted")
        return io.BytesIO(b"tampered candidate archive")

    def stage(*_args):
        if failure == "candidate":
            version.python.unlink()
        elif failure == "malformed-profile":
            profile = core.read_json(version.profile)
            profile["profile"] = "wrong-profile"
            write_json(version.profile, profile)
        elif failure in {"incompatible-profile-protocol", "incompatible-profile-schema"}:
            profile = core.read_json(version.profile)
            key = "protocol_version" if failure.endswith("protocol") else "schema_version"
            profile[key] = "incompatible"
            write_json(version.profile, profile)
        elif failure == "missing-source":
            (version.root / "a").rmdir()
        elif failure == "missing-environment":
            profile = core.read_json(version.profile)
            profile["runtime_environment"] = str(version.root / "missing-py")
            write_json(version.profile, profile)
        elif failure == "missing-service":
            (version.root / "services.json").unlink()
        elif failure == "invalid-service":
            write_json(version.root / "services.json", {"version": 1, "services": [{
                "name": "pack-host", "argv": [], "restart": "always",
            }]})
        elif failure == "missing-pack-host":
            write_json(version.root / "services.json", {"version": 1, "services": [{
                "name": "chat", "argv": ["/usr/bin/python", "-c", "pass"], "restart": "always",
            }]})
            return version
        if failure in {"malformed-profile", "incompatible-profile-protocol", "incompatible-profile-schema",
                       "missing-source", "missing-environment", "missing-service",
                       "invalid-service", "missing-pack-host"}:
            return version
        tools.fetch_archive("https://example.invalid/candidate.zip", version.root / "tool",
                            sha256="0" * 64)
        pytest.fail("invalid download was accepted")

    monkeypatch.setattr(tools.urllib.request, "urlopen", response)
    monkeypatch.setattr(backend, "stage", stage)
    with pytest.raises((OSError, ValueError), match="download interrupted|checksum mismatch|incomplete|profile|directory|service|incompatible"):
        core.Installer(backend.root, data, backend, say=lambda _line: None).run("update", {})

    assert core.current(backend.root) == old
    assert {path.name: path.read_bytes() for path in data.iterdir()} == before
    assert backend.service.selection.read_bytes() == selection_before
    assert not (backend.root / "update-transaction.json").exists()
    for name in ("stop", "backup", "upgrade", "register"):
        getattr(backend, name).assert_not_called()


def test_regular_update_admits_supported_older_schema_and_reaches_migration(candidate, monkeypatch):
    from runtime_protocol.store import RealmStore

    backend, candidate_version, data, realm = candidate
    old_root = backend.root / "versions" / "old"
    old_root.mkdir()
    old_python = old_root / "py" / "bin" / "python"
    old_python.parent.mkdir(parents=True)
    old_python.touch()
    old = Version(old_root, old_python, old_root / "profile.json", {}, release=17)
    write_json(old_root / "version.json", old.document())
    core.switch(backend.root, old)
    data.mkdir()
    (data / "work.bin").write_bytes(b"existing-workspace")
    original_workspace = (data / "work.bin").read_bytes()
    realm_root = Path(realm["realm_root"])
    RealmStore.initialize(realm_root, realm_id=realm["realm_id"]).close()
    database = realm_root / "realm.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE timelines DROP COLUMN name")
        connection.execute("DROP TABLE user_app_state")
        connection.execute("UPDATE runtime_schema SET version=29 WHERE id=1")
    realm_before = database.read_bytes()
    calls = []

    monkeypatch.setattr(backend, "workspace", lambda *_args: realm.copy())
    monkeypatch.setattr(backend, "has_workspace", lambda _data: True)
    monkeypatch.setattr(backend, "stage", lambda *_args: candidate_version)
    monkeypatch.setattr(backend, "stop", lambda version, _data: calls.append(("stop", version)))
    monkeypatch.setattr(backend, "backup", lambda *_args: calls.append(("backup",)) or None)
    monkeypatch.setattr(backend, "upgrade", lambda version, _data: calls.append(("upgrade", version)))
    monkeypatch.setattr(backend, "register", lambda version, _data: calls.append(("register", version)))
    monkeypatch.setattr(backend, "start", lambda version, _data: calls.append(("start", version)))
    monkeypatch.setattr(backend, "verify", lambda *_args: None)
    monkeypatch.setattr(backend, "sync_skills", lambda *_args: None)
    monkeypatch.setattr(backend, "finish", lambda *_args: None)
    monkeypatch.setattr(backend, "open", lambda: None)
    monkeypatch.setattr(backend, "install_launcher", lambda *_args: None, raising=False)
    monkeypatch.setattr(core, "install_launcher", lambda *_args: None, raising=False)
    # The selected candidate must have the same durable version record as a staged build.
    write_json(candidate_version.root / "version.json", candidate_version.document())

    core.Installer(backend.root, data, backend, say=lambda _line: None).run("update", {})

    assert ("upgrade", candidate_version) in calls
    assert core.current(backend.root).root == candidate_version.root
    assert (data / "work.bin").read_bytes() == original_workspace
    assert database.read_bytes() == realm_before


def test_journal_uses_durable_lf_writer_with_windows_translation_and_missing_home(tmp_path, monkeypatch):
    from runtime_protocol import platform

    monkeypatch.delenv("HOME", raising=False)
    original_open = Path.open
    original_replace = platform.durable_replace
    replacements = []

    def windows_writer(path, mode="r", *args, **kwargs):
        if mode == "w":
            kwargs.setdefault("newline", "\r\n")
        return original_open(path, mode, *args, **kwargs)

    def replace(source, destination):
        replacements.append((source, destination))
        original_replace(source, destination)

    monkeypatch.setattr(Path, "open", windows_writer)
    monkeypatch.setattr(platform, "durable_replace", replace)
    installer = core.Installer(tmp_path, tmp_path / "data", SimpleNamespace())
    installer._save_update({"operation_id": "a" * 32}, "staged")

    journal = tmp_path / "update-transaction.json"
    assert replacements == [(tmp_path / "update-transaction.json.tmp", journal)]
    assert b"\r\n" not in journal.read_bytes()
    assert core.read_json(journal)["step"] == "staged"
