"""An app-only restart never backs up, migrates, or restores the workspace."""

import contextlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrid.installer import app_update as update
from astrid.installer.core import Version, current, read_json, switch, write_json
from astrid.installer.service import LABEL


@pytest.fixture
def installed(tmp_path, monkeypatch):
    root, data = tmp_path / "app" / "astrid", tmp_path / "workspace"
    calls = []

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

    backend = SimpleNamespace(
        service=SimpleNamespace(home=tmp_path, system=sys.platform, recorded=lambda: record.copy()),
        workspace=lambda *_: realm.copy(),
        stage=lambda *_: calls.append("stage") or candidate,
        persist_identity=persist_identity,
        run=lambda *_args, **_kwargs: json.dumps(schema),
    )

    def start(value, *_args):
        assert record["identity"] == value.document()["identity"]
        calls.append("start:" + value.root.name)

    lifecycle = SimpleNamespace(
        node=lambda v: Path(v.env["ASTRID_NODE_EXECUTABLE"]),
        validate=lambda v: None,
        running=lambda *_: True,
        stop=lambda v, *_: calls.append("stop:" + v.root.name),
        start=start,
    )
    return SimpleNamespace(**locals())


def run(env):
    update.update_app(env.root, env.backend, {}, lifecycle=env.lifecycle, say=lambda _: None)


def test_selection_check_is_read_only(installed):
    installed.backend.persist_identity = Mock(side_effect=AssertionError("selection must not write"))

    version, data, record = update.selection(installed.root, installed.backend)

    assert (version, data) == (installed.old, installed.data)
    assert record["identity"] == installed.old.document()["identity"]
    installed.backend.persist_identity.assert_not_called()


def test_stages_online_then_restarts_without_workspace_mutations(installed):
    run(installed)
    assert installed.calls == ["stage", "stop:old", "start:new"]
    assert current(installed.root) == installed.candidate
    assert installed.record["identity"] == installed.candidate.document()["identity"]
    assert installed.record["unrelated"] == {"keep": True}


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


@pytest.mark.parametrize("failure", ["stage", "schema", "selection", "realm"])
def test_preparation_failure_keeps_old_running(installed, failure):
    env = installed
    stage = env.backend.stage

    def fail(*args):
        if failure == "stage":
            raise OSError("disk full")
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


def test_stop_refusal_never_switches_or_starts_candidate(installed):
    def stop(*_):
        raise RuntimeError("active work")

    installed.lifecycle.stop = stop
    with pytest.raises(RuntimeError, match="active work"):
        run(installed)
    assert current(installed.root) == installed.old
    assert installed.calls == ["stage"]
    assert installed.record["identity"] == installed.old.document()["identity"]


def test_controller_cleanup_failure_after_down_restarts_old(installed):
    def stop(*_):
        raise RuntimeError("controller cleanup")

    installed.lifecycle.stop = stop
    installed.lifecycle.running = lambda *_: False
    with pytest.raises(RuntimeError, match="controller cleanup"):
        run(installed)
    assert current(installed.root) == installed.old
    assert installed.calls == ["stage", "start:old"]


def test_rollback_launcher_incompatible_fails_before_stage(installed):
    def validate(_):
        raise ValueError("incompatible old launcher")

    installed.lifecycle.validate = validate
    with pytest.raises(ValueError, match="incompatible old"):
        run(installed)
    assert installed.calls == []


def test_lock_failure_after_stop_restarts_old(installed, monkeypatch):
    real_lock = update.locked

    @contextlib.contextmanager
    def lock(path, **kwargs):
        if path.name != ".app-update.lock":
            raise ValueError("lock unavailable")
        with real_lock(path, **kwargs):
            yield

    monkeypatch.setattr(update, "locked", lock)
    with pytest.raises(ValueError, match="lock unavailable"):
        run(installed)
    assert installed.calls == ["stage", "stop:old", "start:old"]
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


def test_nonterminal_migration_rejected_before_staging(installed):
    write_json(installed.root / "update-transaction.json", {"step": "migrating"})
    with pytest.raises(ValueError, match="interrupted migration"):
        run(installed)
    assert installed.calls == []


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
    assert "inspect_canonical_schema" in argv[3]
    assert "migrate_canonical" not in argv[3]
    assert argv[-2:] == [installed.realm["realm_root"], "realm"]


def test_canonical_supervisor_is_detached_and_readiness_checked(installed, monkeypatch):
    env = installed
    scripts = env.root.parent.parent / "pinokio"
    scripts.mkdir()
    (scripts / "session.mjs").touch()
    (scripts / "lifecycle.mjs").touch()
    (env.root / "run").touch()
    spawned = []
    verified = []
    env.backend.verify = lambda *args, **kwargs: verified.append((args, kwargs))
    def spawn(*args, **kwargs):
        spawned.append((args, kwargs))
        kwargs["stdout"].write(b"ASTRID_PINOKIO_READY\n")
        kwargs["stdout"].flush()
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(update.subprocess, "Popen", spawn)
    lifecycle = update.PinokioLifecycle(env.root, env.backend)
    lifecycle.start(env.candidate, env.data, "realm")
    args, kwargs = spawned[0]
    assert args[0] == [str(lifecycle.node(env.candidate)), str(scripts / "session.mjs"),
                       "serve", str(env.root), str(env.root.parent / "editor"), "backend"]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == update.subprocess.DEVNULL
    assert verified == [((env.candidate, env.data, "realm"), {"timeout": 180})]


def test_cli_requires_explicit_pinokio_mode():
    from astrid.installer.cli import main

    with pytest.raises(SystemExit):
        main(["--update-app"])


@pytest.mark.parametrize("busy", [False, True])
def test_runtime_idle_fence_precedes_any_supervisor_change(installed, busy):
    calls = []
    lifecycle = object.__new__(update.PinokioLifecycle)
    lifecycle.root = installed.root
    lifecycle.lifecycle = installed.root / "lifecycle.mjs"
    lifecycle.node = lambda _: Path("/node")
    lifecycle.running = lambda *_: True

    def runtime(*args, **kwargs):
        calls.append("down")
        assert args == (installed.old, installed.data, "down")
        assert kwargs["profile"] is True
        if busy:
            raise RuntimeError("active work")

    lifecycle.backend = SimpleNamespace(
        runtime=runtime, run=lambda *args, **kwargs: calls.append("controller"))
    if busy:
        with pytest.raises(RuntimeError, match="active work"):
            lifecycle.stop(installed.old, installed.data)
        assert calls == ["down"]
    else:
        lifecycle.stop(installed.old, installed.data)
        assert calls == ["down", "controller"]
