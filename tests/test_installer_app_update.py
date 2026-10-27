"""An app-only restart never backs up, migrates, or restores the workspace."""

import contextlib
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrid.installer import app_update as update
from astrid.installer import core as installer_core
from astrid.installer.core import (
    Installer,
    RuntimeOperationError,
    VerificationError,
    Version,
    current,
    read_json,
    switch,
    write_json,
)
from astrid.installer.service import LABEL


@pytest.fixture
def installed(tmp_path, monkeypatch):
    root, data = tmp_path / "app" / "astrid", tmp_path / "workspace"
    calls = []
    journal_steps = []

    real_write_json = installer_core.write_json

    def observe_write_json(path, value):
        if path.name == "update-transaction.json":
            journal_steps.append(value["step"])
        real_write_json(path, value)

    monkeypatch.setattr(installer_core, "write_json", observe_write_json)
    owner = {"version": "old", "running": True}
    workspace = data / "runtime" / "realm" / "workspace.json"
    workspace.parent.mkdir(parents=True)
    workspace.write_text('{"keep":"workspace"}', encoding="utf-8")

    def version(name, release):
        folder = root / "versions" / name
        folder.mkdir(parents=True)
        python, profile, node = folder / "python", folder / "profile.json", folder / "node"
        python.touch()
        node.touch()
        profile.write_text("{}")
        value = Version(folder, python, profile, {"ASTRID_NODE_EXECUTABLE": str(node)},
                        release=release, managed_by="pinokio")
        write_json(folder / "version.json", value.document())
        return value

    old, candidate = version("old", 11), version("new", 12)
    switch(root, old)
    marker = {"owner": LABEL, "managed_by": "pinokio", "data_root": str(data)}
    record = {**marker, "program_root": str(root), "identity": old.document()["identity"],
              "unrelated": {"keep": True}}
    write_json(root / "install.json", marker)
    realm = {"realm_id": "realm", "realm_root": str(data / "realm")}
    schema = {"identity": {"version": 31, "kind": "v31", "realm_id": "realm"}, "target": 31}

    def persist_identity(value):
        if value.root.parent == root / "versions":
            write_json(value.root / "version.json", value.document())
        record["identity"] = value.document()["identity"]

    candidate_admissions = []

    def validate_candidate(value, *_args):
        candidate_admissions.append(value.root.name)
        if value == candidate and failure_state.get("value") == "candidate-validation":
            raise ValueError("candidate validation failed")

    backend = SimpleNamespace(
        service=SimpleNamespace(home=tmp_path, system=sys.platform, recorded=lambda: record.copy()),
        existing=lambda _data: None,
        has_workspace=lambda _data: False,
        workspace=lambda *_: realm.copy(),
        stage=lambda *_: calls.append("stage") or candidate,
        validate_candidate=validate_candidate,
        stop=lambda *_args: None,
        persist_identity=persist_identity,
        run=lambda *_args, **_kwargs: json.dumps(schema),
        backup=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("app-only update must not back up workspace data")),
        upgrade=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("app-only update must not migrate workspace data")),
        restore=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("app-only update must not restore workspace data")),
        finish=lambda *_args: None,
    )

    def start(value, *_args):
        assert record["identity"] == value.document()["identity"]
        calls.append("start:" + value.root.name)
        owner.update(version=value.root.name, running=True)

    def stop(value, *_args):
        if value == old and journal_steps and journal_steps[-1] == "stopping":
            assert journal_steps[-2:] == ["staged", "stopping"]
            assert read_json(root / "update-transaction.json").get("step") == "stopping"
        calls.append("stop:" + value.root.name)
        owner.update(version=value.root.name, running=False)

    validations = []

    def validate(value):
        validations.append(value.root.name)
        if value == candidate and failure_state.get("value") == "candidate-validation":
            raise ValueError("candidate validation failed")

    failure_state = {"value": None}

    lifecycle = SimpleNamespace(
        node=lambda v: Path(v.env["ASTRID_NODE_EXECUTABLE"]),
        validate=validate,
        running=lambda *_: owner["running"],
        stop=stop,
        start=start,
    )
    return SimpleNamespace(**locals())


def run(env):
    update.update_app(env.root, env.backend, {}, lifecycle=env.lifecycle, say=lambda _: None)


@pytest.mark.parametrize("mode", ["migration", "app", "repair"])
def test_admission_is_durable_before_staged_and_stop(installed, monkeypatch, mode):
    from runtime_protocol import platform

    env = installed
    replaced = []
    replace = platform.durable_replace

    def publish(source, target):
        if target != env.root / "run_runtime.py":
            return replace(source, target)
        assert env.journal_steps == []
        assert env.owner["running"]
        replace(source, target)
        replaced.append(target)

    monkeypatch.setattr(platform, "durable_replace", publish)
    real_stop = env.lifecycle.stop

    def stop(*args):
        assert replaced == [env.root / "run_runtime.py"]
        compile(replaced[0].read_text(), str(replaced[0]), "exec")
        real_stop(*args)

    env.lifecycle.stop = stop
    env.backend.backup = lambda *_args: {"root": str(env.root / "backups" / "test")}
    env.backend.upgrade = lambda *_args: None
    env.backend.register = lambda *_args: None
    env.backend.sync_skills = lambda *_args: None
    if mode == "app":
        run(env)
    else:
        update.update_pinokio(env.root, env.backend, {"release": 12 if mode == "migration" else 11},
                              lifecycle=env.lifecycle, say=lambda _: None)
    assert read_json(env.root / "update-transaction.json")["step"] == "complete"


@pytest.mark.parametrize("failure", ["write", "replace", "flush"])
def test_runner_publication_failure_aborts_before_destructive_work(installed, monkeypatch, failure):
    from runtime_protocol import platform

    env = installed
    runner = env.root / "run_runtime.py"
    runner.write_text("# retained runner\n")
    before = env.workspace.read_bytes()

    def fail(*_args):
        raise OSError("publication failed")

    if failure == "write":
        monkeypatch.setattr("astrid.installer.launcher.os.fsync", fail)
    elif failure == "flush":
        monkeypatch.setattr(platform, "flush_replace", fail)
    else:
        monkeypatch.setattr(platform, "durable_replace", fail)
    with pytest.raises(OSError, match="publication failed"):
        run(env)
    assert env.calls == ["stage"]
    assert env.journal_steps == []
    assert current(env.root) == env.old
    assert env.owner == {"version": "old", "running": True}
    assert env.record["identity"] == env.old.document()["identity"]
    assert env.workspace.read_bytes() == before
    assert list(env.root.glob(".run_runtime.py.*")) == []
    if failure == "flush":
        compile(runner.read_text(), str(runner), "exec")
        assert "<Astrid foreground launcher>" in runner.read_text()
    else:
        assert runner.read_text() == "# retained runner\n"
    # A fresh attempt must publish successfully even after a post-rename error.
    monkeypatch.undo()
    run(env)
    assert read_json(env.root / "update-transaction.json")["step"] == "complete"


@pytest.mark.parametrize("failure", [False, True])
def test_recovery_publishes_admission_before_first_stop(installed, monkeypatch, failure):
    env = installed
    write_json(env.root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "d" * 32,
        "transaction_kind": "app", "step": "stopping", "data_root": str(env.data),
        "old": env.old.document(), "candidate": env.candidate.document(),
        "realm": env.realm, "backup": None,
    })
    events = []
    publish = update.publish_runtime_runner

    def prepare(root):
        assert env.calls == []
        if failure:
            raise OSError("publication failed")
        publish(root)
        events.append("published")

    monkeypatch.setattr(update, "publish_runtime_runner", prepare)
    stop = env.lifecycle.stop

    def stopped(*args):
        assert events == ["published"]
        stop(*args)

    env.lifecycle.stop = stopped
    if failure:
        with pytest.raises(OSError, match="publication failed"):
            update.recover_app(env.root, env.backend, lifecycle=env.lifecycle, say=lambda _: None)
        assert env.calls == []
        assert env.owner == {"version": "old", "running": True}
        assert current(env.root) == env.old
        assert read_json(env.root / "update-transaction.json")["step"] == "stopping"
        return
    update.recover_app(env.root, env.backend, lifecycle=env.lifecycle, say=lambda _: None)
    assert env.calls == ["stop:old", "start:old"]
    assert read_json(env.root / "update-transaction.json")["step"] == "rolled_back"


def test_ordinary_start_refuses_update_publication_and_stop_handoffs(installed, monkeypatch):
    env = installed
    monkeypatch.delenv("ASTRID_INSTALLER_OPERATION_ID", raising=False)
    runner = env.root / "run_runtime.py"
    monkeypatch.setattr(sys, "argv", [str(runner)])
    refusals = []

    def ordinary_start(boundary):
        with pytest.raises(ValueError, match="Another Astrid operation"):
            runpy.run_path(str(runner))
        refusals.append(boundary)

    publish, stop = update.publish_runtime_runner, env.lifecycle.stop

    def prepare(root):
        publish(root)
        assert not env.journal_steps
        ordinary_start("published")

    def stopping(*args):
        assert read_json(env.root / "update-transaction.json")["step"] == "stopping"
        ordinary_start("stopping")
        stop(*args)

    monkeypatch.setattr(update, "publish_runtime_runner", prepare)
    env.lifecycle.stop = stopping
    run(env)
    assert refusals == ["published", "stopping"]
    # Terminal transactions release ordinary Start to the selected command.
    class ExecCalled(Exception):
        pass

    def execute(_file, arguments, _environment):
        assert arguments[0] == str(env.candidate.python)
        raise ExecCalled

    monkeypatch.setattr("os.execvpe", execute)
    with pytest.raises(ExecCalled):
        runpy.run_path(str(runner))


def test_unified_pinokio_migration_uses_shared_transaction_and_readiness_gate(installed):
    """Migration updates use C2's journal and retain the old workspace until ready."""
    data_bytes = installed.workspace.read_bytes()
    events = []

    def backup(_old, _data, _realm):
        events.append("backup")
        return {"root": str(installed.root / "backups" / "test")}

    def upgrade(candidate, _data):
        events.append("migrate")
        installed.workspace.write_text('{"keep":"migrated"}', encoding="utf-8")

    def restore(_old, _data, _realm, _backup):
        events.append("restore")
        installed.workspace.write_bytes(data_bytes)

    def register(candidate, _data):
        events.append("register:" + candidate.root.name)

    def sync(candidate):
        events.append("sync:" + candidate.root.name)

    installed.backend.backup = backup
    installed.backend.upgrade = upgrade
    installed.backend.restore = restore
    installed.backend.register = register
    installed.backend.sync_skills = sync
    installed.backend.finish = lambda *_args: events.append("finish")
    original_start = installed.lifecycle.start

    def start(candidate, *args):
        if candidate == installed.candidate:
            journal = read_json(installed.root / "update-transaction.json")
            assert journal["transaction_kind"] == "migration"
            assert journal["step"] == "switched"
            assert current(installed.root) == candidate
            assert installed.workspace.read_text(encoding="utf-8") == '{"keep":"migrated"}'
        events.append("ready:" + candidate.root.name)
        return original_start(candidate, *args)

    installed.lifecycle.start = start
    update.update_pinokio(
        installed.root, installed.backend, {"release": 12}, lifecycle=installed.lifecycle,
        say=lambda _message: None, force=True,
    )

    journal = read_json(installed.root / "update-transaction.json")
    assert journal["transaction_kind"] == "migration"
    assert journal["step"] == "complete"
    assert installed.journal_steps[:3] == ["staged", "stopping", "stopped"]
    assert "backed_up" in installed.journal_steps
    assert "migrated" in installed.journal_steps
    assert installed.journal_steps.index("verified") < installed.journal_steps.index("complete")
    assert events == ["backup", "migrate", "register:new", "ready:new", "sync:new", "finish"]
    assert current(installed.root) == installed.candidate
    assert installed.workspace.read_text(encoding="utf-8") == '{"keep":"migrated"}'


def test_unified_pinokio_migration_readiness_failure_restores_workspace_and_old_owner(installed):
    data_bytes = installed.workspace.read_bytes()
    events = []

    installed.backend.backup = lambda *_args: {"root": str(installed.root / "backups" / "test")}

    def upgrade(_candidate, _data):
        installed.workspace.write_text('{"keep":"migrated"}', encoding="utf-8")

    def restore(_old, _data, _realm, _backup):
        events.append("restore")
        installed.workspace.write_bytes(data_bytes)

    installed.backend.upgrade = upgrade
    installed.backend.restore = restore
    installed.backend.register = lambda *_args: events.append("register")
    installed.backend.sync_skills = lambda *_args: events.append("sync")
    installed.backend.finish = lambda *_args: events.append("finish")
    real_start = installed.lifecycle.start

    def fail_candidate(candidate, *args):
        if candidate == installed.candidate:
            events.append("candidate-not-ready")
            raise ValueError("candidate readiness failed")
        events.append("old-ready")
        return real_start(candidate, *args)

    installed.lifecycle.start = fail_candidate
    with pytest.raises(ValueError, match="candidate readiness failed"):
        update.update_pinokio(
            installed.root, installed.backend, {"release": 12}, lifecycle=installed.lifecycle,
            say=lambda _message: None, force=True,
        )

    journal = read_json(installed.root / "update-transaction.json")
    assert journal["step"] == "rolled_back"
    assert journal["transaction_kind"] == "migration"
    assert current(installed.root) == installed.old
    assert installed.record["identity"] == installed.old.document()["identity"]
    assert installed.workspace.read_bytes() == data_bytes
    assert events == ["register", "candidate-not-ready", "restore", "old-ready"]
    assert "complete" not in installed.journal_steps


def test_pinokio_update_release_dispatch_repairs_without_staging_old_candidate(installed):
    installed.backend.register = lambda *_args: None
    installed.backend.sync_skills = lambda *_args: None
    update.update_pinokio(
        installed.root, installed.backend, {"release": 10}, lifecycle=installed.lifecycle,
        say=lambda _message: None,
    )

    assert installed.calls == ["stop:old", "start:old"]
    assert installed.journal_steps[-1] == "complete"
    assert read_json(installed.root / "update-transaction.json")["transaction_kind"] == "app"
    assert current(installed.root) == installed.old
    assert installed.candidate_admissions == ["old"]


def test_pinokio_update_routes_existing_install_through_unified_policy(installed, monkeypatch):
    calls = []
    monkeypatch.setattr(update, "update_pinokio", lambda *args, **kwargs: calls.append((args, kwargs)))
    installer = Installer(installed.root, installed.data, installed.backend,
                          managed_by="pinokio", say=lambda _message: None)

    installer.run("update", {"release": 12})

    assert len(calls) == 1
    assert calls[0][0] == (installed.root, installed.backend, {"release": 12})
    assert calls[0][1]["force"] is True
    assert calls[0][1]["say"] is not None


def test_selection_check_is_read_only(installed):
    installed.backend.persist_identity = Mock(side_effect=AssertionError("selection must not write"))

    version, data, record = update.selection(installed.root, installed.backend)

    assert (version, data) == (installed.old, installed.data)
    assert record["identity"] == installed.old.document()["identity"]
    installed.backend.persist_identity.assert_not_called()


def test_stages_online_then_restarts_without_workspace_mutations(installed):
    run(installed)
    assert installed.calls == ["stage", "stop:old", "start:new"]
    assert installed.validations == ["old", "new"]
    assert installed.journal_steps[:2] == ["staged", "stopping"]
    assert installed.journal_steps[-1] == "complete"
    assert not {"backed_up", "migrated"}.intersection(installed.journal_steps)
    assert read_json(installed.root / "update-transaction.json")["transaction_kind"] == "app"
    assert current(installed.root) == installed.candidate
    assert installed.record["identity"] == installed.candidate.document()["identity"]
    assert installed.record["unrelated"] == {"keep": True}
    assert installed.workspace.read_text(encoding="utf-8") == '{"keep":"workspace"}'
    assert installed.candidate_admissions == ["new"]


def test_full_candidate_admission_precedes_pinokio_candidate_validation(installed):
    validate = installed.lifecycle.validate

    def check_order(value):
        if value == installed.candidate:
            assert installed.candidate_admissions == ["new"]
        validate(value)

    installed.lifecycle.validate = check_order
    run(installed)


def test_app_update_recovers_pending_app_transaction_before_new_stage(installed, monkeypatch):
    write_json(installed.root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "a" * 32,
        "transaction_kind": "app", "step": "stopping", "data_root": str(installed.data),
        "old": installed.old.document(), "candidate": installed.candidate.document(),
        "realm": installed.realm, "backup": None,
    })
    held = set()
    real_lock = update.locked

    @contextlib.contextmanager
    def lock(path, **kwargs):
        held.add(path.name)
        try:
            with real_lock(path, **kwargs):
                yield
        finally:
            held.remove(path.name)

    monkeypatch.setattr(update, "locked", lock)

    def start(version, *_args):
        assert held == {".app-update.lock"}
        assert installed.record["identity"] == version.document()["identity"]
        installed.calls.append("start:" + version.root.name)
        installed.owner.update(version=version.root.name, running=True)

    installed.lifecycle.start = start
    run(installed)

    assert installed.calls == ["stop:old", "start:old", "stage", "stop:old", "start:new"]
    assert installed.validations == ["old", "old", "new"]
    assert installed.journal_steps[:3] == ["recovering", "rollback_selected", "rolled_back"]
    assert installed.journal_steps[-1] == "complete"
    assert current(installed.root) == installed.candidate
    assert installed.record["identity"] == installed.candidate.document()["identity"]
    assert installed.workspace.read_text(encoding="utf-8") == '{"keep":"workspace"}'


def test_app_recovery_stops_selected_candidate_after_switch(installed):
    switch(installed.root, installed.candidate)
    installed.backend.persist_identity(installed.candidate)
    write_json(installed.root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "c" * 32,
        "transaction_kind": "app", "step": "switched", "data_root": str(installed.data),
        "old": installed.old.document(), "candidate": installed.candidate.document(),
        "realm": installed.realm, "backup": None,
    })

    run(installed)

    assert installed.calls == ["stop:new", "start:old", "stage", "stop:old", "start:new"]
    assert current(installed.root) == installed.candidate
    assert installed.record["identity"] == installed.candidate.document()["identity"]
    assert installed.workspace.read_text(encoding="utf-8") == '{"keep":"workspace"}'


def test_app_recovery_reconciles_old_identity_when_pointer_already_rolled_back(installed):
    installed.backend.persist_identity(installed.candidate)
    write_json(installed.root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "e" * 32,
        "transaction_kind": "app", "step": "recovering", "data_root": str(installed.data),
        "old": installed.old.document(), "candidate": installed.candidate.document(),
        "realm": installed.realm, "backup": None,
    })

    run(installed)

    assert installed.calls == ["stop:old", "start:old", "stage", "stop:old", "start:new"]
    assert current(installed.root) == installed.candidate
    assert installed.record["identity"] == installed.candidate.document()["identity"]


def test_cli_allows_pending_app_transaction_through_selection(tmp_path, monkeypatch):
    from runtime_protocol import platform

    from astrid.installer import app_update, cli

    home, root, data = tmp_path / "home", tmp_path / "app" / "astrid", tmp_path / "workspace"
    home.mkdir(exist_ok=True)
    python, profile = root / "versions" / "old" / "python", root / "versions" / "old" / "profile.json"
    python.parent.mkdir(parents=True)
    python.touch()
    profile.write_text("{}", encoding="utf-8")
    old = Version(python.parent, python, profile, {}, release=11, managed_by="pinokio")
    write_json(old.root / "version.json", old.document())
    switch(root, old)
    marker = {"owner": LABEL, "managed_by": "pinokio", "data_root": str(data)}
    write_json(root / "install.json", marker)
    record = {**marker, "program_root": str(root), "identity": old.document()["identity"]}
    write_json(root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "b" * 32,
        "transaction_kind": "app", "step": "stopping", "data_root": str(data),
        "old": old.document(), "candidate": old.document(),
        "realm": {"realm_id": "realm", "realm_root": str(data / "realm")}, "backup": None,
    })
    uv, manifest = tmp_path / "uv", tmp_path / "manifest.json"
    uv.touch()
    manifest.write_text("{}", encoding="utf-8")
    backend = SimpleNamespace(service=SimpleNamespace(recorded=lambda: record.copy()))
    called = []

    class CliBackend:
        def __new__(cls, *_args, **_kwargs):
            return backend

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(platform, "private_directory", lambda path: Path(path).mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(cli, "Backend", CliBackend)
    monkeypatch.setattr(cli, "validate_manifest", lambda _value: {"pinned": True})
    monkeypatch.setattr(cli, "download", lambda *_args: pytest.fail("manifest should be read from disk"))
    monkeypatch.setattr(app_update, "recover_app", lambda *args, **kwargs: called.append((args, kwargs)))
    monkeypatch.setattr(app_update, "update_app", lambda *args, **kwargs: called.append((args, kwargs)))

    result = cli.main([
        "--update-app", "--managed-by", "pinokio", "--root", str(root),
        "--manifest", str(manifest), "--uv", str(uv),
    ])

    assert result == 0
    assert len(called) == 1
    assert called[0][0][0] == root
    assert called[0][0][1] is backend


def test_normal_installer_refuses_while_app_update_owns_shared_lock(installed):
    journal = installed.root / "update-transaction.json"
    previous = {"step": "complete", "sentinel": "keep"}
    write_json(journal, previous)

    with update.locked(installed.root / ".app-update.lock"):
        with pytest.raises(ValueError, match="installation lock"):
            Installer(installed.root, installed.data, installed.backend, managed_by="pinokio").run(
                "update", {},
            )

    assert installed.calls == []
    assert current(installed.root) == installed.old
    assert read_json(journal) == previous


def test_pinokio_update_refuses_nested_app_update_while_shared_lock_is_held(installed, monkeypatch):
    monkeypatch.setattr(update, "PinokioLifecycle", lambda *_args: installed.lifecycle)
    journal = installed.root / "update-transaction.json"
    previous = {"step": "complete", "sentinel": "keep"}
    write_json(journal, previous)
    stage = installed.backend.stage
    nested_failures = []

    def stage_then_try_app_update(*args):
        value = stage(*args)
        try:
            run(installed)
        except ValueError as failure:
            nested_failures.append(failure)
            raise
        return value

    installed.backend.stage = stage_then_try_app_update
    with pytest.raises(ValueError, match="installation lock"):
        Installer(installed.root, installed.data, installed.backend, managed_by="pinokio").run(
            "update", {},
        )

    assert len(nested_failures) == 1
    assert installed.calls == ["stage"]
    assert current(installed.root) == installed.old
    assert read_json(journal) == previous
    assert installed.journal_steps == []


def test_busy_details_survive_failure_to_terminalize_journal(installed, monkeypatch):
    details = {"kind": "busy", "blockers": [{"task_id": "task-7", "state": "running"}]}
    failure = RuntimeOperationError("Astrid is busy", details=details, stop_refused=True)
    installed.lifecycle.stop = Mock(side_effect=failure)
    real_write_json = installer_core.write_json

    def fail_stop_refusal_journal(path, value):
        if path.name == "update-transaction.json" and value.get("outcome") == "stop_refused":
            raise OSError("journal is read-only")
        real_write_json(path, value)

    monkeypatch.setattr(installer_core, "write_json", fail_stop_refusal_journal)
    with pytest.raises(RuntimeOperationError) as raised:
        run(installed)

    assert raised.value is failure
    assert raised.value.details == details
    assert raised.value.stop_refused is True
    assert any("Could not durably cancel" in note for note in getattr(raised.value, "__notes__", []))
    assert current(installed.root) == installed.old
    assert installed.owner == {"version": "old", "running": True}
    assert read_json(installed.root / "update-transaction.json")["step"] == "stopping"


def test_app_update_migrates_legacy_selected_identity_before_activation(installed):
    legacy = installed.old.document()
    legacy.pop("identity")
    write_json(installed.old.root / "version.json", legacy)
    write_json(installed.old.root / "manifest.json", {"release": 11})
    installed.record.pop("identity")

    run(installed)

    assert installed.record["identity"] == installed.candidate.document()["identity"]
    assert read_json(installed.old.root / "version.json")["identity"] == installed.old.document()["identity"]


def test_selection_identity_change_during_staging_refuses_before_stop(installed):
    changed = {"release": 99, "managed_by": "pinokio"}

    def stage(*_args):
        installed.record["identity"] = changed.copy()
        return installed.candidate

    installed.backend.stage = stage

    with pytest.raises(ValueError, match="selected installation changed while preparing"):
        run(installed)

    assert installed.calls == []
    assert current(installed.root) == installed.old
    assert installed.record["identity"] == changed


@pytest.mark.parametrize("failure", ["download", "hash", "candidate-validation", "schema", "selection", "realm"])
def test_preparation_failure_keeps_old_running(installed, failure):
    env = installed
    stage = env.backend.stage
    env.failure_state["value"] = failure

    def fail(*args):
        if failure == "download":
            raise OSError("download failed")
        if failure == "hash":
            raise ValueError("candidate hash mismatch")
        candidate = stage(*args)
        if failure == "schema":
            env.schema["target"] = 32
        if failure == "selection":
            env.record["data_root"] = str(env.data / "different")
        if failure == "realm":
            env.realm["realm_id"] = "different"
        return candidate

    env.backend.stage = fail
    with pytest.raises((ValueError, OSError)):
        run(env)
    assert not any(call.startswith(("stop:", "start:")) for call in env.calls)
    assert current(env.root) == env.old
    assert env.record["identity"] == env.old.document()["identity"]
    assert env.owner == {"version": "old", "running": True}
    assert env.workspace.read_text(encoding="utf-8") == '{"keep":"workspace"}'
    assert env.workspace.is_file()
    if failure == "candidate-validation":
        assert env.candidate_admissions == ["new"]
        assert env.validations == ["old"]


def test_start_failure_restores_only_app_pointer_and_relaunches_old(installed):
    def start(version, *_):
        assert installed.record["identity"] == version.document()["identity"]
        installed.calls.append("start:" + version.root.name)
        if version == installed.candidate:
            raise RuntimeError("unhealthy")

    installed.lifecycle.start = start
    with pytest.raises(RuntimeError, match="unhealthy"):
        run(installed)
    assert current(installed.root) == installed.old
    assert installed.calls == ["stage", "stop:old", "start:new", "stop:new", "start:old"]
    assert installed.record["identity"] == installed.old.document()["identity"]
    assert installed.record["unrelated"] == {"keep": True}


def test_generic_runtime_start_error_is_not_a_stop_refusal(installed):
    def start(version, *_):
        installed.calls.append("start:" + version.root.name)
        if version == installed.candidate:
            raise RuntimeOperationError("candidate Runtime failed", details={"kind": "startup"})

    installed.lifecycle.start = start
    with pytest.raises(RuntimeOperationError, match="candidate Runtime failed") as raised:
        run(installed)

    assert raised.value.stop_refused is False
    assert current(installed.root) == installed.old
    assert installed.calls == ["stage", "stop:old", "start:new", "stop:new", "start:old"]


@pytest.mark.parametrize("category", ["busy", "timeout", "identity"])
def test_stop_refusal_never_probes_status_switches_or_starts_candidate(installed, category):
    details = {"kind": category, "blockers": [{"task_id": "task-1"}]}

    def stop(*_):
        raise RuntimeOperationError("Astrid is busy (1 tasks running). Try again when they finish.",
                                    details=details, stop_refused=True)

    installed.lifecycle.stop = stop
    installed.lifecycle.running = Mock(side_effect=AssertionError("must not probe after refusal"))
    with pytest.raises(RuntimeOperationError) as raised:
        run(installed)
    assert raised.value.details == details
    installed.lifecycle.running.assert_not_called()
    assert current(installed.root) == installed.old
    assert installed.calls == ["stage"]
    assert installed.record["identity"] == installed.old.document()["identity"]
    assert installed.owner == {"version": "old", "running": True}
    assert installed.journal_steps[-1] == "complete"
    assert read_json(installed.root / "update-transaction.json")["outcome"] == "stop_refused"


def test_controller_cleanup_failure_after_down_restarts_old(installed):
    stop_calls = []

    def stop(version, *_):
        stop_calls.append(version)
        if len(stop_calls) == 1:
            installed.owner.update(version=version.root.name, running=False)
            raise RuntimeOperationError("controller cleanup", stop_refused=False, stop_completed=True)
        installed.owner.update(version=version.root.name, running=False)
        installed.calls.append("stop:" + version.root.name)

    installed.lifecycle.stop = stop
    with pytest.raises(RuntimeOperationError, match="controller cleanup"):
        run(installed)
    assert current(installed.root) == installed.old
    assert installed.calls == ["stage", "stop:old", "start:old"]


def test_candidate_stop_refusal_does_not_switch_or_relaunch(installed):
    def start(version, *_):
        installed.calls.append("start:" + version.root.name)
        if version == installed.candidate:
            raise RuntimeError("candidate failed readiness")

    def stop(version, *_args):
        installed.calls.append("stop:" + version.root.name)
        if version == installed.candidate:
            raise RuntimeOperationError("Astrid is busy during rollback", details={"task_count": 1},
                                        stop_refused=True)

    installed.lifecycle.start = start
    installed.lifecycle.stop = stop

    with pytest.raises(RuntimeOperationError, match="busy during rollback") as raised:
        run(installed)

    assert raised.value.details == {"task_count": 1}
    assert current(installed.root) == installed.candidate
    assert installed.calls == ["stage", "stop:old", "start:new", "stop:new"]


def test_rollback_launcher_incompatible_fails_before_stage(installed):
    def validate(_):
        raise ValueError("incompatible old launcher")

    installed.lifecycle.validate = validate
    with pytest.raises(ValueError, match="incompatible old"):
        run(installed)
    assert installed.calls == []


def test_lock_failure_after_stop_restarts_old(installed, monkeypatch):
    real_lock = update.locked
    failed = False

    @contextlib.contextmanager
    def lock(path, **kwargs):
        nonlocal failed
        if path.name != ".app-update.lock":
            if not failed:
                failed = True
                raise ValueError("lock unavailable")
        with real_lock(path, **kwargs):
            yield

    monkeypatch.setattr(update, "locked", lock)
    with pytest.raises(ValueError, match="lock unavailable"):
        run(installed)
    assert installed.calls == ["stage", "stop:old", "stop:old", "start:old"]
    assert current(installed.root) == installed.old
    assert installed.record["identity"] == installed.old.document()["identity"]


def test_start_runs_after_lifetime_lock_released(installed, monkeypatch):
    held = set()

    @contextlib.contextmanager
    def lock(path, **kwargs):
        held.add(path.name)
        try:
            yield
        finally:
            held.remove(path.name)

    monkeypatch.setattr(update, "locked", lock)

    def start(*_):
        assert held == {".app-update.lock"}

    installed.lifecycle.start = start
    run(installed)


def test_pinokio_app_update_recovers_staged_migration_journal_before_staging(installed):
    write_json(installed.root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "d" * 32,
        "transaction_kind": "migration", "step": "staged", "data_root": str(installed.data),
        "old": installed.old.document(), "candidate": installed.candidate.document(),
        "realm": installed.realm, "backup": None,
    })

    run(installed)

    assert installed.journal_steps[0] == "complete"  # staged is pre-stop and safe to close.
    assert installed.journal_steps.index("complete") < installed.journal_steps.index("staged")
    assert installed.calls == ["stage", "stop:old", "start:new"]


def test_second_operator_is_rejected(installed):
    with update.locked(installed.root / ".app-update.lock"):
        with pytest.raises(ValueError, match="lock"):
            run(installed)
    assert installed.calls == []


def test_schema_inspection_uses_candidate_readonly_layout_api(installed):
    argv = []

    def execute(args, **kwargs):
        argv.extend(args)
        assert kwargs["extra_env"] == installed.candidate.env
        return json.dumps(installed.schema)

    installed.backend.run = execute
    update.inspect_schema(installed.backend, installed.candidate, installed.realm)
    assert argv[0] == installed.candidate.python
    assert "inspect_canonical_schema" in argv[5]
    assert "migrate_canonical" not in argv[5]
    assert argv[-2:] == [installed.realm["realm_root"], "realm"]


def test_canonical_supervisor_is_detached_and_readiness_checked(installed, monkeypatch):
    env = installed
    scripts = env.root.parent.parent / "pinokio"
    scripts.mkdir()
    (scripts / "session.mjs").touch()
    (scripts / "lifecycle.mjs").touch()
    (env.root / "run").touch()
    runner = env.root / "run_runtime.py"
    runner.write_text("# prepared admission\n")
    spawned = []
    verified = []
    intents = []
    operation_id = "d" * 32
    env.backend.transaction_operation_id = lambda required=False: operation_id
    env.backend._transaction_stop_context = ({"operation_id": operation_id}, None, False)
    env.backend._transaction_launch_intent = lambda purpose, **details: intents.append((purpose, details))
    env.backend.verify = lambda *args, **kwargs: verified.append((args, kwargs))
    def spawn(*args, **kwargs):
        spawned.append((args, kwargs))
        kwargs["stdout"].write(b"ASTRID_PINOKIO_READY\n")
        kwargs["stdout"].flush()
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(update.subprocess, "Popen", spawn)
    lifecycle = update.PinokioLifecycle(env.root, env.backend)
    lifecycle.start(env.candidate, env.data, "realm")
    assert runner.read_text() == "# prepared admission\n"
    assert (env.root / "run").read_bytes() == b""
    assert not (env.root / "run.cmd").exists()
    args, kwargs = spawned[0]
    assert args[0][:3] == [str(lifecycle.node(env.candidate)), "--input-type=module", "-e"]
    assert "runSession({action:'serve'" in args[0][3]
    assert "ASTRID_INSTALLER_OPERATION_ID" in args[0][3]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == update.subprocess.DEVNULL
    assert kwargs["env"]["ASTRID_INSTALLER_OPERATION_ID"] == operation_id
    assert intents == [("pinokio_start", {"version": env.candidate})]
    assert verified == [((env.candidate, env.data, "realm"), {"timeout": 180})]


def test_local_choice_starts_session_with_editor_preparation_and_local_readiness(installed, monkeypatch):
    env = installed
    scripts = env.root.parent.parent / "pinokio"
    scripts.mkdir()
    (scripts / "session.mjs").touch()
    (scripts / "lifecycle.mjs").touch()
    (env.root / "run").touch()
    local_script = env.root.parent / "editor" / "scripts" / "dev-local-workspace.mjs"
    local_script.parent.mkdir(parents=True)
    local_script.touch()
    write_json(env.root.parent / "install-mode.json", {"schema_version": 1, "mode": "local"})
    spawned, verified = [], []
    env.backend.verify = lambda *args, **kwargs: verified.append((args, kwargs))

    def spawn(*args, **kwargs):
        spawned.append((args, kwargs))
        kwargs["stdout"].write(b"ASTRID_PINOKIO_READY\n")
        kwargs["stdout"].flush()
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(update.subprocess, "Popen", spawn)
    lifecycle = update.PinokioLifecycle(env.root, env.backend)
    lifecycle.start(env.candidate, env.data, "realm")

    session_script = spawned[0][0][0][3]
    assert "mode:\"local\"" in session_script
    assert "prepareEditor:true" in session_script
    assert verified == [((env.candidate, env.data, "realm"), {"timeout": 180, "hosted": False})]


def test_local_verification_uses_ready_pack_host_and_exact_transaction_binding(
        installed, monkeypatch):
    from astrid.installer.backend import Backend

    env = installed
    backend = Backend(env.root, env.tmp_path / "install.log", env.tmp_path / "uv", managed_by="pinokio")
    operation_id = "e" * 32
    backend._transaction_stop_context = ({"operation_id": operation_id}, None, False)
    version = Version(
        env.old.root, env.old.python, env.old.profile,
        {**env.old.env, "ASTRID_RUNTIME_DOOR_PORT": "19991"},
        env.old.release, "pinokio",
    )
    status = {
        "health": {
            "status": "ok", "runtime_instance_id": "instance-local",
            "services": [{"name": "pack-host", "state": "running"}],
        },
        "discovery": {"runtime_instance_id": "instance-local"},
        "support": {"pid_alive": True},
    }
    backend.runtime = lambda *_args, **_kwargs: status
    runtime = env.data / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    write_json(runtime / "discovery.json", {
        "active_realm": "realm", "runtime_instance_id": "instance-local",
        "installer_operation_id": operation_id,
    })
    write_json(runtime / "instance.lock", {"installer_operation_id": operation_id})
    (runtime / "generic-host.ready.json").write_text("{}", encoding="utf-8")
    opener = SimpleNamespace(open=lambda *_args, **_kwargs: pytest.fail(
        "Local verification must not contact the hosted browser door"))
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: opener)

    backend.verify(version, env.data, "realm", timeout=1, hosted=False)


def test_local_verification_rejects_mismatched_transaction_binding(installed, monkeypatch):
    from astrid.installer.backend import Backend

    env = installed
    backend = Backend(env.root, env.tmp_path / "install.log", env.tmp_path / "uv", managed_by="pinokio")
    backend._transaction_stop_context = ({"operation_id": "f" * 32}, None, False)
    version = env.old
    status = {
        "health": {
            "status": "ok", "runtime_instance_id": "instance-local",
            "services": [{"name": "pack-host", "state": "running"}],
        },
        "discovery": {"runtime_instance_id": "instance-local"},
        "support": {"pid_alive": True},
    }
    backend.runtime = lambda *_args, **_kwargs: status
    runtime = env.data / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    write_json(runtime / "discovery.json", {
        "active_realm": "realm", "runtime_instance_id": "instance-local",
        "installer_operation_id": "a" * 32,
    })
    write_json(runtime / "instance.lock", {"installer_operation_id": "a" * 32})
    (runtime / "generic-host.ready.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: SimpleNamespace(
        open=lambda *_a, **_k: pytest.fail("Local verification must not contact the hosted browser door")))

    with pytest.raises(VerificationError, match="operation binding"):
        backend.verify(version, env.data, "realm", timeout=0.02, hosted=False)


def test_cli_requires_explicit_pinokio_mode():
    from astrid.installer.cli import main

    with pytest.raises(SystemExit):
        main(["--update-app"])


@pytest.mark.parametrize("failure", [None, "busy", "timeout", "identity"])
def test_runtime_handoff_precedes_controller_cleanup(installed, failure):
    calls = []
    lifecycle = object.__new__(update.PinokioLifecycle)
    lifecycle.root = installed.root
    lifecycle.lifecycle = installed.root / "lifecycle.mjs"
    lifecycle.node = lambda _: Path("/node")

    def runtime_handoff(version, data):
        calls.append("fence")
        assert version == installed.old
        assert data == installed.data
        if failure:
            raise RuntimeOperationError("Runtime stop refused", details={"kind": failure}, stop_refused=True)

    lifecycle.backend = SimpleNamespace(
        stop=runtime_handoff,
        run=lambda *args, **kwargs: calls.append("controller"))
    if failure:
        with pytest.raises(RuntimeOperationError, match="Runtime stop refused"):
            lifecycle.stop(installed.old, installed.data)
        assert calls == ["fence"]
    else:
        lifecycle.stop(installed.old, installed.data)
        assert calls == ["fence", "controller"]


def test_pinokio_controller_failure_reports_completed_runtime_stop(installed):
    lifecycle = object.__new__(update.PinokioLifecycle)
    lifecycle.root = installed.root
    lifecycle.lifecycle = installed.root / "lifecycle.mjs"
    lifecycle.node = lambda _: Path("/node")

    lifecycle.backend = SimpleNamespace(
        stop=lambda _version, _data: None,
        run=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("controller cleanup failed")),
    )

    with pytest.raises(RuntimeOperationError, match="controller cleanup failed") as raised:
        lifecycle.stop(installed.old, installed.data)

    assert raised.value.stop_completed is True
    assert raised.value.stop_refused is False


@pytest.mark.parametrize("controller", ["exited", "remaining", "replacement"])
def test_controller_exit_during_authenticated_stop_preserves_other_owners(installed, controller):
    lifecycle = object.__new__(update.PinokioLifecycle)
    lifecycle.root = installed.root
    lifecycle.lifecycle = installed.root / "lifecycle.mjs"
    lifecycle.node = lambda _: Path("/node")
    record = installed.root / "pinokio-control.json"
    original = {"pid": 123, "token": "old"}
    write_json(record, original)
    calls = []

    def request_stop(*_args, **_kwargs):
        assert calls == ["fence"]
        calls.append("controller")
        if controller == "exited":
            record.unlink()
        elif controller == "replacement":
            write_json(record, {"pid": 456, "token": "replacement"})
        raise update.subprocess.CalledProcessError(1, ["/node"], stderr="fetch failed")

    lifecycle.backend = SimpleNamespace(
        stop=lambda *_args: calls.append("fence"), run=request_stop,
    )
    if controller == "exited":
        lifecycle.stop(installed.old, installed.data)
        assert not record.exists()
    else:
        with pytest.raises(RuntimeOperationError) as raised:
            lifecycle.stop(installed.old, installed.data)
        assert raised.value.stop_completed is True
        assert raised.value.stop_refused is False
        assert read_json(record) == (
            original if controller == "remaining" else {"pid": 456, "token": "replacement"})
    assert calls == ["fence", "controller"]
