"""Update ordering against canonical v31 data and target Runtime v33 tooling."""

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.installer import core, service
from astrid.installer.backend import Backend
from runtime_protocol.store import RealmStore, SCHEMA_VERSION
from runtime_protocol.upgrade import inspect_canonical_schema
from tests.installer_schema_fixtures import remove_round1b_schema_objects


@pytest.mark.parametrize("source_version", [31, 33])
def test_update_uses_target_for_inspection_and_never_runs_old_after_migration(tmp_path, monkeypatch, source_version):
    assert SCHEMA_VERSION == 33
    root, data = tmp_path / "program", tmp_path / "data"
    realm_root = data / "runtime" / "realms" / "realm-1"
    RealmStore.initialize(realm_root, realm_id="realm-1").close()
    if source_version == 31:
        with sqlite3.connect(realm_root / "realm.sqlite3") as connection:
            remove_round1b_schema_objects(connection)
            connection.execute("UPDATE runtime_schema SET version=31 WHERE id=1")
    core.write_json(data / "runtime" / "catalog.json", {
        "version": 1, "selected_realm_id": "realm-1", "source_profiles": {},
        "realms": [{"realm_id": "realm-1", "display_name": "Astrid", "data_root": str(realm_root)}],
    })
    assert inspect_canonical_schema(realm_root)["version"] == source_version
    work = data / "work.bin"
    work.write_bytes(b"preserved user data")
    events = []
    old_root, new_root = root / "versions" / "old", root / "versions" / "target"
    old_root.mkdir(parents=True)
    old_python = old_root / "python"
    old_python.write_text("predecessor interpreter fixture", encoding="utf-8")
    old = core.Version(old_root, old_python, old_root / "profile.json", {}, release=1)
    core.write_json(old_root / "version.json", old.document())
    core.switch(root, old)
    for name in ("r", "a", "py/bin"):
        (new_root / name).mkdir(parents=True, exist_ok=True)
    python = new_root / "py/bin/python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    profile = new_root / "profile.json"
    core.write_json(new_root / "services.json", {"version": 1, "services": [{
        "name": "pack-host", "argv": [sys.executable, "-c", "pass"], "restart": "always",
    }]})
    core.write_json(profile, {"profile": "astrid", "runtime_checkout": str(new_root / "r"),
                             "source_checkout": str(new_root / "a"), "runtime_environment": str(new_root / "py"),
                             "service_declaration": str(new_root / "services.json")})
    candidate = core.Version(new_root, python, profile, {}, release=2)
    core.write_json(new_root / "version.json", candidate.document())

    class FixtureBackend(Backend):
        def run(self, argv, **kwargs):
            if Path(argv[0]) == old.python:
                with sqlite3.connect(realm_root / "realm.sqlite3") as connection:
                    version = connection.execute("SELECT version FROM runtime_schema WHERE id=1").fetchone()[0]
                events.append(("old-tool", version))
                if version > 31:
                    raise RuntimeError("predecessor rejects schema newer than v31")
                raise AssertionError("predecessor inspection is unnecessary")
            if len(argv) > 5 and "inspect_canonical_schema" in str(argv[5]):
                events.append(("target-inspect",))
            return super().run(argv, **kwargs)

        def stage(self, *_args):
            events.append(("stage",))
            return candidate

        def stop(self, version, _data):
            events.append(("stop", version.root.name))

        def backup(self, version, _data, realm):
            assert version == old
            assert inspect_canonical_schema(realm_root)["version"] == 31
            events.append(("backup",))
            destination = root / "backups" / "snapshot"
            destination.mkdir(parents=True)
            shutil.copy2(realm_root / "realm.sqlite3", destination / "realm.sqlite3")
            return {"root": str(destination), "kind": "fixture"}

        def upgrade(self, version, _data):
            assert version == candidate
            events.append(("target-migrate",))
            script = ("from runtime_protocol.upgrade import migrate_canonical_to_current; import sys; "
                      "migrate_canonical_to_current(sys.argv[1], expected_realm_id='realm-1', "
                      "confirmation='MIGRATE CANONICAL realm-1')")
            self.run([version.python, "-X", "utf8", "-I", "-c", script, str(realm_root)], extra_env=version.env)

        def register(self, version, _data):
            events.append(("register", version.root.name))

        def start(self, *_args): pass
        def verify(self, *_args, **_kwargs): pass
        def sync_skills(self, *_args): pass
        def finish(self, *_args): pass
        def open(self): pass
        def publish_launch_admission(self, *_args): pass

    backend = FixtureBackend(root, tmp_path / "install.log", tmp_path / "uv", say=lambda _line: None)
    real_switch = core.switch
    def observed_switch(program_root, version):
        events.append(("switch", version.root.name))
        real_switch(program_root, version)
    monkeypatch.setattr(core, "switch", observed_switch)
    monkeypatch.setattr(core, "install_launcher", lambda *_args: None)
    monkeypatch.setattr(core, "install_runtime", lambda *_args: None)

    core.Installer(root, data, backend, say=lambda _line: None).run("update", {"release": 2})

    assert core.current(root) == candidate
    assert inspect_canonical_schema(realm_root)["version"] == 33
    assert work.read_bytes() == b"preserved user data"
    assert not any(event[0] == "old-tool" for event in events)
    inspect_index = events.index(("target-inspect",))
    stop_index = events.index(("stop", "old"))
    switch_index = events.index(("switch", "target"))
    assert events.index(("stage",)) < inspect_index < stop_index < switch_index
    if source_version == 31:
        assert stop_index < events.index(("backup",)) < events.index(("target-migrate",))
        assert events.index(("target-migrate",)) < events.index(("stop", "target")) < switch_index
        assert not any(event[0] == "old-tool" for event in events[events.index(("target-migrate",)) + 1:])
    else:
        assert not any(event[0] in {"backup", "target-migrate"} for event in events)


@pytest.mark.parametrize("selection", ["missing", "multiple"])
def test_target_catalog_refuses_unselected_or_ambiguous_workspace(tmp_path, selection):
    data = tmp_path / "data"
    first, second = data / "first", data / "second"
    first.mkdir(parents=True)
    second.mkdir()
    realms = [{"realm_id": "first", "display_name": "First", "data_root": str(first)}]
    if selection == "multiple":
        realms.append({"realm_id": "second", "display_name": "Second", "data_root": str(second)})
    core.write_json(data / "runtime/catalog.json", {
        "version": 1, "selected_realm_id": None if selection == "missing" else "first",
        "realms": realms, "source_profiles": {},
    })
    target = core.Version(tmp_path / "target", Path(sys.executable), tmp_path / "profile.json", {})
    backend = Backend(tmp_path / "program", tmp_path / "install.log", tmp_path / "uv")
    with pytest.raises(subprocess.CalledProcessError):
        backend.update_workspace(target, data)


@pytest.mark.parametrize("system", ["darwin", "linux", "win32"])
def test_service_descriptor_selects_target_after_switch(tmp_path, system):
    target = core.Version(tmp_path / "target", tmp_path / "target/python", tmp_path / "target/profile.json", {})
    descriptor = service.render(system, target, tmp_path / "data", tmp_path / "install.log", user="test-user")
    assert str(target.python) in descriptor
    assert str(target.profile) in descriptor
