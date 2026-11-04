"""Copy transaction fault tests. No test starts or signals a Runtime process.

Successful adoption here is an explicit future-Runtime contract fixture; the
pinned CLI rejection test below guards the real de98a5aa boundary separately.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer import core
from astrid.installer.backend import Backend
from astrid.installer.copy_update import (JOURNAL, data_fields, fingerprint, rollback,
                                          selected_data, warn_divergence)
from astrid.installer.service import LABEL, Service, launch_admission, render


class PowerLoss(BaseException):
    pass


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root, source = tmp_path / "program", tmp_path / "workspace"
    realm = source / "runtime" / "realms" / "realm-1"
    realm.mkdir(parents=True)
    (realm / "owner.lock").touch()
    (realm / "empty").mkdir()
    (realm / "media.bin").write_bytes(b"user-media" * 400)
    with sqlite3.connect(realm / "realm.sqlite3") as db:
        db.execute("CREATE TABLE projects (name TEXT)")
        db.execute("INSERT INTO projects VALUES ('Original project')")
        db.execute("CREATE TABLE tasks (name TEXT)")
    core.write_json(source / "runtime" / "catalog.json", {
        "selected_realm_id": "realm-1", "realms": [{"realm_id": "realm-1", "data_root": str(realm)}]})
    versions = []
    for name, release in (("old", 0), ("candidate", 1)):
        folder = root / "versions" / name
        python = folder / "py" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.touch()
        profile = folder / "profile.json"
        profile.write_text("{}")
        version = core.Version(folder, python, profile,
            {"ASTRID_RUNTIME_DATA_ROOT": str(source if release == 0 else source.with_name(source.name + '-r1'))}, release)
        core.write_json(folder / "version.json", version.document())
        versions.append(version)
    old, candidate = versions
    core.switch(root, old)
    core.write_json(root / "install.json", {"owner": LABEL, "data_root": str(source)})
    selection = tmp_path / "install-selection.json"
    core.write_json(selection, {"owner": LABEL, "data_root": str(source), "program_root": str(root),
        "realm_id": "realm-1", "realm_root": str(realm)})
    service_path, env_file = tmp_path / "R0.service", tmp_path / "service-env.json"
    service_path.write_bytes((Path(__file__).parent / "fixtures/release0/services/systemd.service").read_bytes())
    env_file.write_bytes(b'{}\n')
    manager_calls, stops, messages = [], [], []
    service = SimpleNamespace(path=service_path, env_file=env_file, selection=selection, system="linux",
        log=(tmp_path / "runtime-service.log"),
        recorded=lambda: core.read_json(selection),
        require_stopped_for_copy=lambda data: manager_calls.append(("observe", data)),
        systemd=lambda: False)
    backend = Backend(root, tmp_path / "install.log", Path(sys.executable))
    backend.service = service
    backend.stage = lambda manifest, program, data: candidate
    backend._discovery_owner_state = lambda marker: "stale"
    backend.open = lambda: None
    backend.stop = lambda version, data: stops.append((version, data))
    backend.start = lambda version, data: None
    backend.verify = lambda *args, **kwargs: None
    runtime_calls = []

    def runtime(version, data, *args, **kwargs):
        runtime_calls.append((version, data, args, kwargs))
        assert args[:2] == ("workspace", "adopt")
        assert "--read-only-source" in args and "--no-start" in args
        target = data / "runtime" / "realms" / "realm-1"
        shutil.copytree(realm, target)
        # A safe adopter creates private writable destination storage even
        # when the source directory/file modes are read-only.
        for path in (target, *target.rglob("*")):
            path.chmod(0o700 if path.is_dir() else 0o600)
        core.write_json(data / "runtime" / "catalog.json", {"selected_realm_id": "realm-1",
            "realms": [{"realm_id": "realm-1", "data_root": str(target)}]})
        return {"ok": True, "source_unchanged": True, "started": False,
            "source": str(realm), "destination": str(target), "realm_id": "realm-1"}
    backend.runtime = runtime

    def register(version, data):
        core.write_json(selection, {**service.recorded(), **data_fields(service.recorded(), data),
            "realm_id": "realm-1", "realm_root": str(data / "runtime" / "realms" / "realm-1"),
            "identity": version.document()["identity"], "service_settings": {}, "chat_settings": {}})
        service_path.write_bytes(render("linux", version, data, service.log, env_file=env_file).encode())
    backend.register = register
    installer = core.Installer(root, source, backend, say=messages.append)
    return SimpleNamespace(**locals())


def recover(case):
    core.Installer(case.root, case.source, case.backend, say=case.messages.append)._recover_update()


def assert_source(case, before):
    assert fingerprint(case.source) == before
    with sqlite3.connect(f"file:{case.realm / 'realm.sqlite3'}?immutable=1", uri=True) as db:
        assert db.execute("SELECT name FROM projects").fetchall() == [("Original project",)]


def test_upgrade_readonly_source_and_identical_manifest(installation):
    c = installation
    for path in (c.source, *c.source.rglob("*")):
        path.chmod(0o500 if path.is_dir() else 0o400)
    before = fingerprint(c.source)
    legacy = (c.root / "install.json").read_bytes()
    c.installer._run_copy_update(c.old, {"release": 1})
    assert_source(c, before)
    assert core.current(c.root) == c.candidate
    record = c.service.recorded()
    assert record["data_root"] == str(c.source)
    assert record["r1_data_root"] == str(c.source) + "-r1"
    assert core.read_json(c.root / "install.json")["data_root"] == str(c.source)
    assert core.read_json(c.root / JOURNAL)["step"] == "complete"
    assert not (c.root / "update-transaction.json").exists()
    assert f"Your previous version is kept at {c.source}" in c.messages
    assert c.stops == []
    saved = core.read_json(c.root / JOURNAL)["saved_service"]
    assert base64.b64decode(next(e for e in saved if e["path"] == str(c.root / "install.json"))["bytes"]) == legacy
    assert launch_admission(c.candidate, c.installer.data) is False


@pytest.mark.parametrize("mutation", ["earlier-file", "new-file", "symlink", "selection"])
def test_mutation_during_adopt_never_switches_or_overwrites_external_edits(installation, mutation):
    c = installation
    original = c.backend.runtime
    def changing(*args, **kwargs):
        result = original(*args, **kwargs)
        if mutation == "earlier-file":
            (c.realm / "media.bin").write_bytes(b"changed after copy")
        elif mutation == "new-file":
            (c.realm / "late-write").write_text("new task")
        elif mutation == "symlink":
            (c.realm / "new-link").symlink_to(c.realm / "media.bin")
        else:
            c.service_path.write_bytes(b"external edited unit\n")
            core.write_json(c.selection, {"owner": "other"})
        return result
    c.backend.runtime = changing
    with pytest.raises(ValueError):
        c.installer._run_copy_update(c.old, {"release": 1})
    assert core.current(c.root) == c.old
    assert c.stops == []
    assert (Path(str(c.source) + "-r1") / "runtime/realms/realm-1/media.bin").read_bytes() == b"user-media" * 400
    if mutation == "selection":
        assert c.service_path.read_bytes() == b"external edited unit\n"
        assert c.service.recorded() == {"owner": "other"}


@pytest.mark.parametrize("step", ["staged", "adopting", "adopted", "switching", "switched", "activating"])
def test_crash_each_adopt_switch_intent_preserves_source_and_recovers(installation, step):
    c = installation
    before, original_unit = fingerprint(c.source), c.service_path.read_bytes()
    save = c.installer._save_update
    def interrupted(transaction, phase, **details):
        save(transaction, phase, **details)
        if phase == step:
            raise PowerLoss()
    c.installer._save_update = interrupted
    with pytest.raises(PowerLoss):
        c.installer._run_copy_update(c.old, {"release": 1})
    assert_source(c, before)
    recover(c)
    assert_source(c, before)
    assert core.current(c.root) == c.old
    assert c.service_path.read_bytes() == original_unit
    assert not c.service.recorded().get("r1_data_root")
    assert c.stops == []  # No new Runtime has been started in any of these phases.
    assert core.read_json(c.root / JOURNAL)["step"] == "rolled_back"
    recover(c)  # terminal replay is a no-op


def test_powerloss_inside_adopt_and_after_pointer_write(installation, monkeypatch):
    c = installation
    before = fingerprint(c.source)
    switch = core.switch
    def crash(root, version):
        switch(root, version)
        if version == c.candidate:
            raise PowerLoss()
    monkeypatch.setattr(core, "switch", crash)
    with pytest.raises(PowerLoss):
        c.installer._run_copy_update(c.old, {"release": 1})
    assert core.current(c.root) == c.candidate
    recover(c)
    assert core.current(c.root) == c.old
    assert_source(c, before)


def test_rollback_after_r1_accepted_task_keeps_both_roots(installation):
    c = installation
    before, original_unit = fingerprint(c.source), c.service_path.read_bytes()
    target = Path(str(c.source) + "-r1")
    def accept_task(*args, **kwargs):
        with sqlite3.connect(target / "runtime/realms/realm-1/realm.sqlite3") as db:
            db.execute("INSERT INTO tasks VALUES ('R1 accepted task')")
        raise RuntimeError("readiness failed after accepted task")
    c.backend.verify = accept_task
    with pytest.raises(RuntimeError, match="readiness failed"):
        c.installer._run_copy_update(c.old, {"release": 1})
    assert_source(c, before)
    assert c.stops == [(c.candidate, target)]
    assert core.current(c.root) == c.old
    assert c.service_path.read_bytes() == original_unit
    with sqlite3.connect(target / "runtime/realms/realm-1/realm.sqlite3") as db:
        assert db.execute("SELECT name FROM tasks").fetchall() == [("R1 accepted task",)]
    with pytest.raises(ValueError, match="already exists"):
        c.installer._run_copy_update(c.old, {"release": 1})


def test_crash_during_rollback_can_resume_after_selection_restored(installation, monkeypatch):
    from astrid.installer import copy_update
    c = installation
    before = fingerprint(c.source)
    c.backend.verify = lambda *args: (_ for _ in ()).throw(RuntimeError("failed"))
    restore = copy_update.restore_service
    def crash(backend, saved):
        restore(backend, saved)
        raise PowerLoss()
    monkeypatch.setattr(copy_update, "restore_service", crash)
    with pytest.raises(PowerLoss):
        c.installer._run_copy_update(c.old, {"release": 1})
    assert core.read_json(c.root / JOURNAL)["candidate_stopped"] is True
    monkeypatch.setattr(copy_update, "restore_service", restore)
    recover(c)
    assert len(c.stops) == 1
    assert core.current(c.root) == c.old
    assert_source(c, before)


def test_copy_divergence_warns_and_never_merges(installation):
    c = installation
    c.installer._run_copy_update(c.old, {"release": 1})
    target = Path(str(c.source) + "-r1")
    before_target = fingerprint(target)
    (c.realm / "media.bin").write_bytes(b"R0 user went back to old version")
    warn_divergence(c.backend, c.messages.append)
    assert c.messages[-1] == f"Previous workspace changed after the update; it is kept at {c.source}"
    assert fingerprint(target) == before_target


@pytest.mark.parametrize("field,value", [("source_unchanged", False), ("started", True),
    ("started", 0), ("realm_id", "other"), ("source", "/other"), ("destination", "/other")])
def test_adopt_requires_exact_safety_receipt(installation, field, value):
    c = installation
    runtime = c.backend.runtime
    def wrong(*args, **kwargs):
        return {**runtime(*args, **kwargs), field: value}
    c.backend.runtime = wrong
    before = fingerprint(c.source)
    with pytest.raises(core.RuntimeOperationError, match="safe read-only"):
        c.installer._run_copy_update(c.old, {"release": 1})
    assert core.current(c.root) == c.old
    assert c.stops == []
    assert_source(c, before)


def test_locked_or_uncertain_source_refuses_without_signalling(installation, monkeypatch):
    c = installation
    def busy(*args, **kwargs):
        raise BlockingIOError("owned")
    monkeypatch.setattr("runtime_protocol.platform.lock_file", busy)
    with pytest.raises(core.RuntimeOperationError, match="Quit Astrid to finish updating"):
        c.installer._run_copy_update(c.old, {"release": 1})
    assert c.runtime_calls == c.stops == []
    assert not (c.root / JOURNAL).exists()


def test_data_fields_never_labels_unchanged_r0_as_r1(tmp_path):
    source, new = tmp_path / "workspace", tmp_path / "workspace-r1"
    assert data_fields({"data_root": str(source)}, source) == {"data_root": str(source)}
    record = {"data_root": str(source), "r1_data_root": str(new)}
    assert data_fields(record, new) == record
    assert selected_data(record) == str(new)


def test_legacy_pending_migration_refused_without_replay(installation):
    c = installation
    core.write_json(c.root / "update-transaction.json", {"step": "migrated", "backup": {"root": "/old-image"}})
    with pytest.raises(ValueError, match="earlier in-place"):
        c.installer._recover_update()
    assert c.stops == c.runtime_calls == []


def test_daemon_starting_backup_restore_paths_are_removed():
    assert not hasattr(Backend, "backup")
    assert not hasattr(Backend, "restore")
    assert not hasattr(Backend, "_backup_size")


def test_actual_runtime_parser_refuses_required_flags_before_touching_source(installation, tmp_path):
    """No Runtime function is mocked: test the selected checkout's real CLI."""
    import astrid_runtime
    c = installation
    before = fingerprint(c.source)
    target = tmp_path / "rejected-destination"
    runtime_root = Path(astrid_runtime.__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(runtime_root),
           "HOME": str(tmp_path / "isolated-home"), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-m", "astrid_runtime", "workspace", "adopt",
        "--from", str(c.source), "--realm-id", "realm-1", "--data-root", str(target),
        "--read-only-source", "--no-start", "--json"], env=env, capture_output=True, text=True,
        timeout=20)
    assert result.returncode != 0
    assert "unrecognized arguments: --read-only-source --no-start" in result.stderr
    assert_source(c, before)
    assert not target.exists()


@pytest.mark.parametrize("filename", ["run", "run_astrid.py", "service-env", "install.json"])
def test_rollback_preserves_external_edits_to_every_target(installation, filename):
    c = installation
    before = fingerprint(c.source)
    edited = c.env_file if filename == "service-env" else c.root / filename
    def external_edit(*args):
        edited.write_bytes(b"external user edit")
        raise RuntimeError("candidate failed")
    c.backend.verify = external_edit
    with pytest.raises(ValueError, match="refusing to overwrite"):
        c.installer._run_copy_update(c.old, {"release": 1})
    assert edited.read_bytes() == b"external user edit"
    assert_source(c, before)


def test_restore_does_not_follow_predictable_hardlink_to_r0(installation):
    c = installation
    before = fingerprint(c.source)
    attack = c.service_path.with_name(c.service_path.name + ".r1-rollback.tmp")
    os.link(c.realm / "media.bin", attack)
    # Creating the hardlink changes ctime; freeze the source after adversarial setup.
    before = fingerprint(c.source)
    c.backend.verify = lambda *args: (_ for _ in ()).throw(RuntimeError("failed"))
    with pytest.raises(RuntimeError, match="failed"):
        c.installer._run_copy_update(c.old, {"release": 1})
    assert_source(c, before)
    assert attack.read_bytes() == b"user-media" * 400


def test_real_register_failure_does_not_publish_partial_identity(installation, monkeypatch):
    import astrid.installer.backend as backend_module
    c = installation
    before = fingerprint(c.source)
    original = backend_module.persist_install_identity
    def crash_after_metadata(version, program_root, **kwargs):
        original(version, program_root, **kwargs)
        assert kwargs.get("selection") is None
        raise RuntimeError("after immutable metadata")
    monkeypatch.setattr(backend_module, "persist_install_identity", crash_after_metadata)
    c.backend.register = lambda version, data: Backend.register(c.backend, version, data)
    with pytest.raises(RuntimeError, match="immutable metadata"):
        c.installer._run_copy_update(c.old, {"release": 1})
    assert core.current(c.root) == c.old
    assert core.read_json(c.root / JOURNAL)["step"] == "rolled_back"
    assert not c.service.recorded().get("r1_data_root")
    assert_source(c, before)


def test_recovery_rechecks_restarted_candidate_after_durable_stop(installation):
    c = installation
    before = fingerprint(c.source)
    c.backend.verify = lambda *args: (_ for _ in ()).throw(RuntimeError("failed"))
    save = c.installer._save_update
    def crash(transaction, step, **details):
        save(transaction, step, **details)
        if details.get("candidate_stopped"):
            raise PowerLoss()
    c.installer._save_update = crash
    with pytest.raises(PowerLoss):
        c.installer._run_copy_update(c.old, {"release": 1})
    target = Path(str(c.source) + "-r1")
    core.write_json(target / "runtime/discovery.json", {"pid": 234567, "process_birth_id": "new-owner"})
    c.backend._discovery_owner_state = lambda marker: "live"
    with pytest.raises(core.RuntimeOperationError, match="Quit Astrid"):
        recover(c)
    assert core.current(c.root) == c.candidate
    assert len(c.stops) == 1
    assert_source(c, before)


def test_crash_inside_adopt_retains_published_destination(installation):
    c = installation
    before = fingerprint(c.source)
    runtime = c.backend.runtime
    def crash(*args, **kwargs):
        runtime(*args, **kwargs)
        raise PowerLoss()
    c.backend.runtime = crash
    with pytest.raises(PowerLoss):
        c.installer._run_copy_update(c.old, {"release": 1})
    assert core.read_json(c.root / JOURNAL)["step"] == "adopting"
    recover(c)
    assert (Path(str(c.source) + "-r1") / "runtime/realms/realm-1/media.bin").exists()
    assert core.current(c.root) == c.old
    assert_source(c, before)


@pytest.mark.parametrize("state", ["inactive", "active", "activating", "unknown"])
def test_real_systemd_copy_probe_never_stops_running_source(tmp_path, state):
    calls = []
    def run(argv):
        calls.append(argv)
        if "show" in argv:
            return (f"MainPID={'0' if state == 'inactive' else '88'}\n"
                    f"FragmentPath={service.path}\nLoadState=loaded\nActiveState={state}\n"
                    "SubState=dead\nJob=\n")
        raise AssertionError(f"unexpected mutation command: {argv}")
    service = Service(tmp_path / "program", tmp_path / "log", run, home=tmp_path, system="linux")
    service.systemd = lambda: True
    if state == "inactive":
        service.require_stopped_for_copy(tmp_path / "data")
    else:
        with pytest.raises(ValueError, match="Quit Astrid to finish updating"):
            service.require_stopped_for_copy(tmp_path / "data")
    assert len(calls) == 1 and calls[0][:3] == ["systemctl", "--user", "show"]


def test_loaded_idle_launchd_job_refuses_without_bootout(tmp_path):
    calls = []
    service = Service(tmp_path / "program", tmp_path / "log", lambda argv: calls.append(argv) or "state = not running",
                      home=tmp_path, system="darwin")
    with pytest.raises(ValueError, match="Quit Astrid to finish updating"):
        service.require_stopped_for_copy(tmp_path / "data")
    assert len(calls) == 1 and calls[0][:2] == ["launchctl", "print"]


@pytest.mark.parametrize("field", ["chat_settings", "service_settings", "identity"])
def test_rollback_preserves_external_selection_field_edits(installation, field):
    c = installation
    before = fingerprint(c.source)
    def edit(*args):
        core.write_json(c.selection, {**c.service.recorded(), field: {"external": "edit"}})
        raise RuntimeError("candidate failed")
    c.backend.verify = edit
    with pytest.raises(ValueError, match="selected service changed"):
        c.installer._run_copy_update(c.old, {"release": 1})
    assert c.service.recorded()[field] == {"external": "edit"}
    assert_source(c, before)


@pytest.mark.parametrize("target", ["run", "current"])
def test_source_alias_in_selected_program_refuses_before_adoption(installation, target):
    c = installation
    path = c.root / target
    payload = b"old\n" if target == "current" else b"source data"
    alias_source = c.realm / "aliased-data"
    alias_source.write_bytes(payload)
    path.unlink(missing_ok=True)
    os.link(alias_source, path)
    before = fingerprint(c.source)
    with pytest.raises(ValueError, match="private regular|regular file"):
        c.installer._run_copy_update(c.old, {"release": 1})
    assert_source(c, before)
    assert c.runtime_calls == c.stops == []


def test_json_old_predictable_temp_alias_cannot_touch_source(installation):
    c = installation
    alias = c.root / (JOURNAL + ".tmp")
    os.link(c.realm / "media.bin", alias)
    before = fingerprint(c.source)
    c.installer._run_copy_update(c.old, {"release": 1})
    assert_source(c, before)
    assert alias.read_bytes() == b"user-media" * 400


def test_cli_log_alias_refuses_without_source_writes(installation, monkeypatch):
    from astrid.installer import cli, service as service_module
    c = installation
    default_root = c.root.parent / "default-program"
    log_parent = default_root.parent / "astrid-installer-r1"
    log_parent.symlink_to(c.source, target_is_directory=True)
    monkeypatch.setattr(cli, "default_program_root", lambda *_: default_root)
    monkeypatch.setattr(service_module, "install_selection", lambda *_: c.selection)
    before = fingerprint(c.source)
    assert cli.main(["--root", str(c.root), "--doctor"]) == 1
    assert_source(c, before)
    assert not (c.source / "install.log").exists()


def test_public_cli_prefers_r1_root(installation, monkeypatch):
    from astrid.installer import cli, service as service_module
    c = installation
    target = Path(str(c.source) + "-r1")
    core.write_json(c.selection, {**c.service.recorded(), "r1_data_root": str(target)})
    monkeypatch.setattr(cli, "default_program_root", lambda *_: c.root)
    monkeypatch.setattr(service_module, "install_selection", lambda *_: c.selection)
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: c.backend)
    observed = []
    class CaptureInstaller:
        def __init__(self, root, data, backend, **kwargs):
            observed.append(data)
        def run(self, *args, **kwargs):
            pass
    monkeypatch.setattr(cli, "Installer", CaptureInstaller)
    assert cli.main(["--root", str(c.root), "--doctor"]) == 0
    assert observed == [target]


@pytest.mark.parametrize("failure", [False, True])
def test_copy_uses_real_backend_and_service_registration(installation, failure):
    c = installation
    before = fingerprint(c.source)
    c.service.root = c.root
    c.service.say = c.messages.append
    c.service.settings = lambda: core.read_json(c.env_file)
    c.service.register = lambda version, data: Service.register(c.service, version, data)
    c.backend.workspace = lambda version, data: {"realm_id": "realm-1",
        "realm_root": str(data / "runtime/realms/realm-1")}
    c.backend.register = lambda version, data: Backend.register(c.backend, version, data)
    if failure:
        c.backend.verify = lambda *args: (_ for _ in ()).throw(RuntimeError("after real registration"))
        with pytest.raises(RuntimeError, match="after real registration"):
            c.installer._run_copy_update(c.old, {"release": 1})
        assert core.current(c.root) == c.old
    else:
        c.installer._run_copy_update(c.old, {"release": 1})
        assert core.current(c.root) == c.candidate
    assert_source(c, before)


def test_runtime_service_log_hardlink_refuses_without_source_writes(installation, monkeypatch):
    from astrid.installer import cli, service as service_module
    c = installation
    monkeypatch.setattr(cli, "default_program_root", lambda *_: c.root)
    monkeypatch.setattr(service_module, "install_selection", lambda *_: c.selection)
    log_parent = c.root.parent / "astrid-installer-r1"
    log_parent.mkdir()
    os.link(c.realm / "media.bin", log_parent / "runtime-service.log")
    before = fingerprint(c.source)
    assert cli.main(["--root", str(c.root), "--doctor"]) == 1
    assert_source(c, before)


def test_external_version_switch_during_copy_is_preserved(installation):
    c = installation
    runtime = c.backend.runtime
    folder = c.root / "versions" / "other"
    other = core.Version(folder, folder / "py/bin/python", folder / "profile.json", {}, 2)
    core.write_json(folder / "version.json", other.document())
    before = fingerprint(c.source)
    def concurrent(*args, **kwargs):
        result = runtime(*args, **kwargs)
        core.switch(c.root, other)
        return result
    c.backend.runtime = concurrent
    with pytest.raises(ValueError, match="selection changed|selected version changed"):
        c.installer._run_copy_update(c.old, {"release": 1})
    assert core.current(c.root) == other
    assert_source(c, before)


def test_public_fresh_install_uses_sibling_and_permanent_legacy_key(installation, monkeypatch, tmp_path):
    from astrid.installer import cli, service as service_module
    c = installation
    root, legacy = tmp_path / "fresh-program", tmp_path / "fresh-workspace"
    new_selection = tmp_path / "fresh-selection.json"
    c.backend.root = root
    c.service.recorded = lambda: core.read_json(new_selection)
    c.service.selection = new_selection
    c.backend.source_checkout_ownership = lambda *_: False
    c.backend.preflight_stop = lambda *_: None
    monkeypatch.setattr(cli, "default_program_root", lambda *_: root)
    monkeypatch.setattr(service_module, "install_selection", lambda *_: new_selection)
    monkeypatch.setattr(cli, "installer_lock_path", lambda *_: tmp_path / "fresh-install.lock")
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: c.backend)
    monkeypatch.setattr(cli, "validate_manifest", lambda value: {"release": 1})
    manifest = tmp_path / "fresh-manifest.json"
    manifest.write_text("{}")
    observed = []
    class CaptureInstaller:
        def __init__(self, root, data, backend, **kwargs):
            observed.append(data)
        def run(self, *args, **kwargs):
            pass
    monkeypatch.setattr(cli, "Installer", CaptureInstaller)
    assert cli.main(["--root", str(root), "--data-root", str(legacy), "--manifest", str(manifest)]) == 0
    assert observed == [Path(str(legacy) + "-r1")]
    marker = core.read_json(root / "install.json")
    assert marker["data_root"] == str(legacy)
    assert marker["r1_data_root"] == str(legacy) + "-r1"
    assert not legacy.exists()


@pytest.mark.parametrize("adapter", ["pinokio", "local"])
def test_deferred_adapters_refuse_before_reading_legacy_workspace(installation, adapter):
    c = installation
    c.backend.workspace = lambda *_: (_ for _ in ()).throw(AssertionError("R0 interpreter must not run"))
    if adapter == "pinokio":
        from astrid.installer.app_update import selection as select
    else:
        from astrid.installer.local_update import _selection as select
    with pytest.raises(ValueError, match="Quit Astrid"):
        select(c.root, c.backend)
    assert c.runtime_calls == c.stops == []
