"""The fully local launcher hooks into the one shared Runtime transaction."""

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer import core
from astrid.installer import local_update as local
from astrid.installer.core import RuntimeOperationError, Version, current, switch, write_json
from astrid.installer.tools import DEFAULT_OMP_SOURCE


def _manifest(release, agent_sha, runtime_sha):
    return {
        "schema_version": 1, "release": release,
        "agent": {"sha": agent_sha}, "runtime": {"sha": runtime_sha},
        "tools": {**{name: {"version": "20.19.4" if name == "node" else "1.2.3"}
                    for name in ("uv", "python", "bun", "node", "ffmpeg")},
                  "omp": {"source": DEFAULT_OMP_SOURCE}},
    }


def _app(tmp_path):
    prep = tmp_path / "astrid-app-update-test"
    paths = [tmp_path / "checkout", prep / "checkout", prep / "checkout" / "dist",
             prep / "checkout" / "node_modules", prep]
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
    return {
        "cwd": str(paths[0]), "oldSha": "a" * 40, "targetSha": "b" * 40,
        "mainSha": "c" * 40, "release": 14,
        "oldAppBuild": "d" * 40, "oldAppRelease": 13, "previousMode": "preview",
        "manifest": _manifest(14, "b" * 40, "c" * 40),
        "launcherChanged": False, "worktree": str(paths[1]),
        "buildDir": str(paths[2]), "dependenciesDir": str(paths[3]),
        "preparationRoot": str(paths[4]),
        "previousPreviewDirectory": str(tmp_path / "previous-dist"),
        "oldBuildExisted": True, "oldDependenciesExisted": True,
    }


def _installed(tmp_path, monkeypatch, *, busy=False, candidate_failure=False):
    root, data = tmp_path / "program", tmp_path / "workspace"
    root.mkdir()
    data.mkdir()
    write_json(root / "install.json", {"owner": "haus.astrid.runtime", "data_root": str(data)})
    realm = {"realm_id": "realm-id", "realm_root": str(data / "realm")}
    versions = []
    for name, release in (("old", 13), ("candidate", 14)):
        folder = root / "versions" / name
        folder.mkdir(parents=True)
        python, profile = folder / "python", folder / "profile.json"
        python.touch()
        profile.write_text("{}", encoding="utf-8")
        version = Version(folder, python, profile, {}, release=release)
        write_json(folder / "version.json", version.document())
        write_json(folder / "manifest.json", {
            "release": release, "agent": {"sha": ("a" if name == "old" else "b") * 40},
            "runtime": {"sha": ("a" if name == "old" else "c") * 40},
        })
        versions.append(version)
    old, candidate = versions
    switch(root, old)
    owner = {"runtime": "old", "editor": "old", "running": True}
    events = []
    transaction_contexts = []

    def stop(version, _data):
        events.append("runtime-stop:" + version.root.name)
        transaction_contexts.append(core.read_json(root / "update-transaction.json").get("transaction_context"))
        if busy:
            raise RuntimeOperationError("busy", details={"safe": False,
                                                           "blockers": [{"id": "task-1"}]})
        owner.update(runtime=version.root.name, running=False)

    def start(version, _data):
        events.append("runtime-start:" + version.root.name)
        owner.update(runtime=version.root.name, running=True)

    def verify(version, _data, _realm_id, **_kwargs):
        events.append("runtime-verify:" + version.root.name)
        if candidate_failure and version == candidate:
            raise ValueError("candidate runtime failed")

    backend = SimpleNamespace(
        managed_by=None,
        preflight_stop=lambda _data: events.append("preflight"),
        stop=stop, register=lambda version, _data: events.append("register:" + version.root.name),
        start=start, verify=verify,
        runtime=lambda version, _data, *_args, **_kwargs: {
            "health": {"release": version.release, "managed_by": version.managed_by},
        },
        stage=lambda *_args: events.append("stage") or candidate,
        validate_candidate=lambda *_args: events.append("validate-candidate"),
        workspace=lambda *_args: realm.copy(),
        source_checkout_ownership=lambda *_args: False,
        persist_identity=lambda version: events.append("identity:" + version.root.name),
        finish=lambda *_args: events.append("finish"),
        sync_skills=lambda *_args: events.append("sync-skills"),
        backup=lambda *_args: events.append("backup") or {"root": str(root / "backups" / "test")},
        upgrade=lambda version, *_args: events.append("upgrade:" + version.root.name),
        restore=lambda *_args: events.append("restore-workspace"),
    )
    monkeypatch.setattr(local, "_selection", lambda *_args: (current(root), data, {}))

    class Hooks:
        app = _app(tmp_path)

        def call(self, name, version, _realm, **_kwargs):
            events.append("hook:" + name + ":" + (version.root.name if version else "none"))
            if name == "release-app":
                assert core.read_json(root / "update-transaction.json")["step"] in {"complete", "rolled_back"}
            if name == "start-editor":
                owner["editor"] = version.root.name
            elif name == "verify-editor":
                assert owner["editor"] == version.root.name
            elif name == "move-checkout":
                owner["checkout"] = "candidate"
            elif name == "restore-checkout":
                if getattr(self, "fail_restore", False):
                    raise local.LocalUpdateHookError("restore-checkout", "reset --keep refused")
                owner["checkout"] = "old"

    return SimpleNamespace(root=root, data=data, realm=realm, old=old, candidate=candidate,
                           owner=owner, backend=backend, events=events,
                           transaction_contexts=transaction_contexts, hooks=Hooks())


def test_update_uses_shared_transaction_and_persists_app_context_before_stop(tmp_path, monkeypatch):
    env = _installed(tmp_path, monkeypatch)
    result = local.run_update(env.root, env.backend, env.hooks.app["manifest"], env.hooks.app,
                              env.hooks, say=lambda _message: None)

    journal = core.read_json(env.root / "update-transaction.json")
    assert journal["step"] == "complete"
    assert journal["transaction_kind"] == "migration"
    assert journal["transaction_context"] == {"owner": "local-update", "app": env.hooks.app}
    assert env.transaction_contexts[0] == journal["transaction_context"]
    assert current(env.root) == env.candidate
    assert env.owner["runtime"] == env.owner["editor"] == "candidate"
    assert result == {"status": "updated", "release": 14,
                      "targetSha": "b" * 40, "launcherChanged": False}
    assert env.events.index("hook:stop-editor:old") < env.events.index("runtime-stop:old")
    assert env.events.index("hook:move-checkout:candidate") < env.events.index("hook:start-editor:candidate")
    assert env.events.index("runtime-verify:candidate") < env.events.index("hook:start-editor:candidate")
    assert env.events[-2:] == ["finish", "hook:release-app:candidate"]


def test_candidate_failure_rolls_back_checkout_pointer_runtime_and_editor(tmp_path, monkeypatch):
    env = _installed(tmp_path, monkeypatch, candidate_failure=True)
    with pytest.raises(ValueError, match="candidate runtime failed") as failure:
        local.run_update(env.root, env.backend, env.hooks.app["manifest"], env.hooks.app,
                         env.hooks, say=lambda _message: None)
    assert failure.value.step == "verify-runtime"

    assert core.read_json(env.root / "update-transaction.json")["step"] == "rolled_back"
    assert current(env.root) == env.old
    assert env.owner["runtime"] == env.owner["editor"] == "old"
    assert env.owner["checkout"] == "old"
    assert "hook:restore-checkout:old" in env.events
    assert env.events[-1] == "hook:verify-editor:old"


def test_stage_failure_reports_its_step_before_any_stop_or_journal(tmp_path, monkeypatch):
    env = _installed(tmp_path, monkeypatch)

    def fail_stage(*_args):
        raise ValueError("source download failed")

    env.backend.stage = fail_stage
    with pytest.raises(ValueError, match="stage-runtime: source download failed") as failure:
        local.run_update(env.root, env.backend, env.hooks.app["manifest"], env.hooks.app,
                         env.hooks, say=lambda _message: None)

    assert failure.value.step == "stage-runtime"
    assert not (env.root / "update-transaction.json").exists()
    assert env.owner == {"runtime": "old", "editor": "old", "running": True}
    assert "hook:stop-editor:old" not in env.events


def test_failed_rollback_reports_original_and_recovery_steps(tmp_path, monkeypatch):
    env = _installed(tmp_path, monkeypatch, candidate_failure=True)
    env.hooks.fail_restore = True
    with pytest.raises(RuntimeError) as failure:
        local.run_update(env.root, env.backend, env.hooks.app["manifest"], env.hooks.app,
                         env.hooks, say=lambda _message: None)

    assert failure.value.step == "verify-runtime"
    assert "verify-runtime: candidate runtime failed" in str(failure.value)
    assert "restore-checkout: reset --keep refused" in str(failure.value)
    assert core.read_json(env.root / "update-transaction.json")["step"] == "recovering"


def test_same_release_uses_shared_repair_without_staging_runtime(tmp_path, monkeypatch):
    env = _installed(tmp_path, monkeypatch)
    app = dict(env.hooks.app)
    app["release"] = 13
    app["manifest"] = _manifest(13, "a" * 40, "a" * 40)
    env.hooks.app = app
    result = local.run_update(env.root, env.backend, app["manifest"], app,
                              env.hooks, say=lambda _message: None)

    journal = core.read_json(env.root / "update-transaction.json")
    assert journal["step"] == "complete"
    assert journal["transaction_kind"] == "app"
    assert current(env.root) == env.old
    assert "stage" not in env.events
    assert "backup" not in env.events
    assert "sync-skills" in env.events
    assert result["release"] == 13


@pytest.mark.parametrize("bad_field", ["owner", "program_root", "data_root", "managed_by", "realm_root"])
def test_selection_requires_service_and_install_marker_to_agree(tmp_path, bad_field):
    root, data = tmp_path / "program", tmp_path / "workspace"
    root.mkdir()
    data.mkdir()
    marker = {"owner": "haus.astrid.runtime", "data_root": str(data)}
    write_json(root / "install.json", marker)
    folder = root / "versions" / "old"
    folder.mkdir(parents=True)
    python, profile = folder / "python", folder / "profile.json"
    python.touch()
    profile.write_text("{}", encoding="utf-8")
    old = Version(folder, python, profile, {}, release=13)
    write_json(folder / "version.json", old.document())
    switch(root, old)
    record = {"owner": marker["owner"], "program_root": str(root), "data_root": str(data),
              "realm_id": "realm", "realm_root": str(data / "realm")}
    backend = SimpleNamespace(
        managed_by=None,
        service=SimpleNamespace(recorded=lambda: record.copy()),
        source_checkout_ownership=lambda *_args: False,
        workspace=lambda *_args: {"realm_id": "realm", "realm_root": str(data / "realm")},
    )
    selected, selected_data, _identity = local._selection(root, backend)
    assert selected == old
    assert selected_data == data

    bad = dict(record)
    bad[bad_field] = "different-selection"
    backend.service.recorded = lambda: bad
    with pytest.raises(ValueError, match="selected Astrid installation|selected workspace"):
        local._selection(root, backend)


def test_fresh_controller_recovers_local_context_from_shared_journal(tmp_path, monkeypatch):
    env = _installed(tmp_path, monkeypatch)
    switch(env.root, env.candidate)
    pending = {
        "format": "astrid-install-update/v1", "operation_id": "d" * 32,
        "transaction_kind": "app", "step": "switched", "data_root": str(env.data),
        "old": env.old.document(), "candidate": env.candidate.document(),
        "realm": env.realm, "backup": None,
        "transaction_context": {"owner": "local-update", "app": env.hooks.app},
    }
    write_json(env.root / "update-transaction.json", pending)

    # A new hook client/backend has no in-memory transaction state from update.
    fresh_hooks = env.hooks.__class__()
    assert local._recover(env.root, env.backend, fresh_hooks, say=lambda _message: None)
    journal = core.read_json(env.root / "update-transaction.json")
    assert journal["step"] == "rolled_back"
    assert current(env.root) == env.old
    assert env.owner["runtime"] == env.owner["editor"] == "old"
    assert env.owner["checkout"] == "old"
    assert env.events[-3:] == [
        "hook:start-editor:old", "hook:verify-editor:old", "hook:release-app:old",
    ]


def test_final_busy_refusal_restores_editor_and_does_not_move_checkout(tmp_path, monkeypatch):
    env = _installed(tmp_path, monkeypatch, busy=True)
    with pytest.raises(RuntimeOperationError) as failure:
        local.run_update(env.root, env.backend, env.hooks.app["manifest"], env.hooks.app,
                         env.hooks, say=lambda _message: None)
    assert core.is_lifecycle_refusal(failure.value)
    assert failure.value.details == {"safe": False, "blockers": [{"id": "task-1"}]}
    assert failure.value.step == "stop-runtime"
    assert "stop-runtime: busy" in str(failure.value)
    assert current(env.root) == env.old
    assert env.owner["runtime"] == env.owner["editor"] == "old"
    assert "hook:move-checkout:candidate" not in env.events
    assert "hook:start-editor:old" in env.events
    assert "hook:verify-editor:old" in env.events
    assert env.events[-1] == "hook:release-app:old"
    assert core.read_json(env.root / "update-transaction.json")["step"] == "complete"


def test_busy_terminal_write_failure_preserves_refusal_and_skips_app_cleanup(
        tmp_path, monkeypatch):
    env = _installed(tmp_path, monkeypatch, busy=True)
    save_update = core.Installer._save_update

    def fail_stop_refusal_write(self, transaction, step, **details):
        if step == "complete" and details.get("outcome") == "stop_refused":
            raise OSError("journal is read-only")
        return save_update(self, transaction, step, **details)

    monkeypatch.setattr(core.Installer, "_save_update", fail_stop_refusal_write)
    with pytest.raises(RuntimeOperationError) as failure:
        local.run_update(env.root, env.backend, env.hooks.app["manifest"], env.hooks.app,
                         env.hooks, say=lambda _message: None)

    assert core.is_lifecycle_refusal(failure.value)
    assert failure.value.details == {"safe": False, "blockers": [{"id": "task-1"}]}
    assert "journal is read-only" in " ".join(getattr(failure.value, "__notes__", []))
    assert "hook:release-app:old" not in env.events
    assert core.read_json(env.root / "update-transaction.json")["step"] != "complete"


def test_hook_protocol_requires_matching_id_and_ok(tmp_path):
    app = _app(tmp_path)
    output = __import__("io").StringIO()
    client = local.HookClient(app, __import__("io").StringIO('{"id":1,"ok":true}\n'), output)
    client.call("stop-editor", None, {"realm_id": "realm"})
    request = json.loads(output.getvalue())
    assert request["hook"] == "stop-editor"
    assert request["id"] == 1
    assert request["app"] == app
    assert request["version"] is None
    assert "operationId" not in request

    bad = local.HookClient(app, __import__("io").StringIO('{"id":2,"ok":true}\n'), __import__("io").StringIO())
    with pytest.raises(ValueError, match="did not match"):
        bad.call("stop-editor", None, {})

    failed = local.HookClient(
        app, __import__("io").StringIO('{"id":1,"ok":false,"error":"verify-editor: preview did not bind"}\n'),
        __import__("io").StringIO(),
    )
    with pytest.raises(local.LocalUpdateHookError, match="verify-editor: preview did not bind") as error:
        failed.call("verify-editor", None, {})
    assert error.value.step == "verify-editor"


def test_isolated_module_subprocess_uses_only_jsonl_protocol(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / "program"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    code = (
        "import runpy,sys; "
        f"sys.path.insert(0,{str(repository)!r}); "
        f"sys.argv=['local_update','--root',{str(root)!r}]; "
        "runpy.run_module('astrid.installer.local_update',run_name='__main__')"
    )
    environment = dict(os.environ, HOME=str(home), PYTHONPATH="", PYTHONHOME="")
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], cwd=repository, env=environment,
        input='{"action":"recover"}\n', text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=30,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"result": {"status": "no-recovery"}}


def test_local_recovery_refuses_foreign_active_journal_before_backend_setup(tmp_path, monkeypatch):
    pending = {"step": "stopping", "transaction_kind": "migration"}
    write_json(tmp_path / "update-transaction.json", pending)
    monkeypatch.setattr(local, "_backend", lambda _root: pytest.fail("backend should not run"))
    with pytest.raises(local.LocalUpdateStepError, match="owning installer"):
        local.execute({"action": "recover"}, tmp_path, stdin=__import__("io").StringIO(),
                      stdout=__import__("io").StringIO())


def test_recovery_routing_refuses_local_journal_in_ordinary_paths(tmp_path):
    pending = {"step": "stopping", "transaction_kind": "migration",
               "transaction_context": {"owner": "local-update"}}
    transaction = tmp_path / "update-transaction.json"
    write_json(transaction, pending)
    installer = core.Installer(tmp_path, tmp_path / "data", SimpleNamespace())
    with pytest.raises(ValueError, match="local launcher"):
        installer._recover_update()

    from astrid.installer.app_update import _recover_app_locked
    with pytest.raises(ValueError, match="local launcher"):
        _recover_app_locked(tmp_path, SimpleNamespace())


@pytest.mark.parametrize("managed_by", [None, "pinokio"])
def test_execute_uses_existing_lock_order_for_both_install_managers(tmp_path, monkeypatch, managed_by):
    from astrid.installer import app_update

    backend = SimpleNamespace(managed_by=managed_by,
                              service=SimpleNamespace(home=tmp_path, system="linux"))
    events = []

    @contextmanager
    def observe_lock(path, *, timeout=0):
        events.append(("enter", Path(path), timeout))
        yield
        events.append(("exit", Path(path), timeout))

    monkeypatch.setattr(local, "_backend", lambda _root: backend)
    monkeypatch.setattr(app_update, "locked", observe_lock)
    monkeypatch.setattr(local, "installer_lock_path", lambda *_args: tmp_path / "installer.lock")
    monkeypatch.setattr(local, "_execute_locked", lambda *_args, **_kwargs: {"status": "no-recovery"})

    assert local.execute({"action": "recover"}, tmp_path, stdin=__import__("io").StringIO(),
                         stdout=__import__("io").StringIO()) == {"status": "no-recovery"}
    entered = [event[1] for event in events if event[0] == "enter"]
    assert entered == ([tmp_path / ".app-update.lock"] if managed_by == "pinokio" else
                       [tmp_path / "installer.lock", tmp_path / ".app-update.lock"])


def test_pinokio_activation_records_each_launch_before_session_handoff(tmp_path, monkeypatch):
    env = _installed(tmp_path, monkeypatch)
    env.backend.managed_by = "pinokio"
    operation_id = "e" * 32
    transaction = {
        "operation_id": operation_id, "step": "switched",
        "old": env.old.document(), "candidate": env.candidate.document(),
    }
    journal = env.root / "update-transaction.json"

    def save(step, **details):
        transaction.update(step=step, **details)
        write_json(journal, transaction)

    seen = []

    def handoff(name, version, _realm, **details):
        assert name == "pinokio-start"
        assert not details
        durable = core.read_json(journal)
        assert durable["operation_id"] == operation_id
        seen.append(version.root.name)

    monkeypatch.setattr(local, "_verify_selected_identity", lambda *_args: None)
    hooks = SimpleNamespace(call=handoff)
    save("switched")
    local._activate_backend(env.backend, env.candidate, env.data, env.realm, hooks)
    local._activate_backend(env.backend, env.old, env.data, env.realm, hooks)
    assert seen == ["candidate", "old"]
    assert "runtime-verify:candidate" in env.events
    assert "runtime-verify:old" in env.events


def test_protocol_error_preserves_c1_report_without_stop_flags(tmp_path, monkeypatch, capsys):
    details = {"safe": False, "blockers": [{"task_id": "task-1", "kind": "render", "state": "running", "age_seconds": 17}]}
    failure = RuntimeOperationError("Astrid is busy (1 tasks running). task-1", details=details)
    failure.step = "stop-runtime"

    def refuse(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(local, "execute", refuse)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO('{"action":"recover"}\n'))
    assert local.main(["--root", str(tmp_path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["details"] == details
    assert result["step"] == "stop-runtime"
    assert "stop_refused" not in result
    assert "stop_completed" not in result
