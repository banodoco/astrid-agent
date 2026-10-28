"""An app-only restart never backs up, migrates, or restores the workspace."""

import contextlib
import json
import os
import runpy
import shutil
import subprocess
import sys
import venv
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


def _install_test_runtime(tmp_path, name, capability=1):
    """Install a tiny noneditable Runtime as Backend.stage does."""
    import setuptools

    source = tmp_path / f"{name}-runtime-source"
    package = source / "astrid_runtime"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "bootstrap.py").write_text(
        f"PINOKIO_STOP_HANDOFF_VERSION = {capability!r}\n", encoding="utf-8")
    (source / "pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools']\nbuild-backend = 'setuptools.build_meta'\n"
        "[project]\nname = 'astrid-runtime'\nversion = '0.1.0'\n"
        "[tool.setuptools]\npackages = ['astrid_runtime']\n",
        encoding="utf-8",
    )

    environment = tmp_path / f"{name}-python"
    venv.EnvBuilder(with_pip=False).create(environment)
    if sys.platform == "win32":
        python = environment / "Scripts" / "python.exe"
        site = environment / "Lib" / "site-packages"
    else:
        python = environment / "bin" / "python"
        site = next((environment / "lib").glob("python*/site-packages"))

    # The prepared agent environment supplies setuptools only during the build.
    build_site = Path(setuptools.__file__).resolve().parent.parent
    (site / "build-dependencies.pth").write_text(str(build_site) + "\n", encoding="utf-8")
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), "--no-deps",
         "--no-build-isolation", "--no-cache", str(source)],
        check=True, capture_output=True, text=True,
    )
    (site / "build-dependencies.pth").unlink()

    profile = tmp_path / f"{name}-profile.json"
    write_json(profile, {
        "runtime_checkout": str(source),
        "runtime_environment": str(environment),
    })
    selected = Version(tmp_path / name, python, profile, {})
    return SimpleNamespace(source=source, environment=environment, python=python,
                           site=site, profile=profile, selected=selected)


def _qualification_lifecycle(tmp_path, selected_python):
    calls = []

    def execute(argv, **_kwargs):
        calls.append(argv)
        if argv[0] == selected_python:
            return subprocess.run(argv, check=True, capture_output=True, text=True).stdout
        return ""  # The canonical JavaScript validation is covered by caller tests.

    lifecycle = object.__new__(update.PinokioLifecycle)
    lifecycle.root = tmp_path
    lifecycle.session = tmp_path / "session.mjs"
    lifecycle.backend = SimpleNamespace(run=execute)
    lifecycle.mode = lambda: "backend"
    lifecycle.node = lambda _version: tmp_path / "node"
    return lifecycle, calls


def test_selected_interpreter_qualifies_noneditable_runtimes_for_both_versions(tmp_path):
    """Retained and candidate environments qualify their own installed Runtime."""
    for name in ("retained", "candidate"):
        installed = _install_test_runtime(tmp_path, name)
        lifecycle, calls = _qualification_lifecycle(tmp_path, installed.python)

        probe = subprocess.run(
            [installed.python, "-I", "-c",
             "import importlib,importlib.metadata,json; "
             "m=importlib.import_module('astrid_runtime.bootstrap'); "
             "d=importlib.metadata.distribution('astrid-runtime'); "
             "print(json.dumps({'module':m.__file__,'origin':json.loads(d.read_text('direct_url.json'))}))"],
            check=True, capture_output=True, text=True,
        )
        installed_info = json.loads(probe.stdout)
        module = Path(installed_info["module"]).resolve()
        assert installed.source not in module.parents
        assert module.is_relative_to(installed.site.resolve())
        assert installed_info["origin"]["url"] == installed.source.resolve().as_uri()
        assert installed_info["origin"]["dir_info"] == {}

        lifecycle.validate(installed.selected)
        assert calls[-1][0] == installed.python
        assert "-I" in calls[-1]


@pytest.mark.parametrize("capability", [None, True, False, 0, 2])
def test_selected_interpreter_refuses_incapable_runtime_versions(tmp_path, capability):
    installed = _install_test_runtime(tmp_path, "selected", capability=1)
    (installed.site / "astrid_runtime" / "bootstrap.py").write_text(
        f"PINOKIO_STOP_HANDOFF_VERSION = {capability!r}\n", encoding="utf-8")
    lifecycle, _calls = _qualification_lifecycle(tmp_path, installed.python)

    with pytest.raises(subprocess.CalledProcessError) as raised:
        lifecycle.validate(installed.selected)
    assert "cannot safely hand off Pinokio Stop" in raised.value.stderr


def test_selected_interpreter_refuses_wrong_environment_and_unrelated_origin(tmp_path):
    installed = _install_test_runtime(tmp_path, "selected")
    lifecycle, _calls = _qualification_lifecycle(tmp_path, installed.python)
    profile = json.loads(installed.profile.read_text(encoding="utf-8"))

    profile["runtime_environment"] = str(tmp_path / "different-environment")
    write_json(installed.profile, profile)
    with pytest.raises(subprocess.CalledProcessError):
        lifecycle.validate(installed.selected)

    profile["runtime_environment"] = str(installed.environment)
    write_json(installed.profile, profile)
    direct_url = installed.site / "astrid_runtime-0.1.0.dist-info" / "direct_url.json"
    direct_url.write_text(json.dumps({"url": (tmp_path / "unrelated").as_uri(),
                                      "dir_info": {}}), encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError):
        lifecycle.validate(installed.selected)


def test_selected_interpreter_refuses_unrelated_import_package_and_metadata_mismatch(tmp_path):
    installed = _install_test_runtime(tmp_path, "selected")
    lifecycle, _calls = _qualification_lifecycle(tmp_path, installed.python)
    package = installed.site / "astrid_runtime"
    alternate = installed.site / "alternate-runtime"
    package.rename(alternate)
    (installed.site / "unrelated-runtime.pth").write_text(
        str(installed.site / "unrelated-runtime-root") + "\n", encoding="utf-8")
    rogue = installed.site / "unrelated-runtime-root" / "astrid_runtime"
    rogue.mkdir(parents=True)
    (rogue / "__init__.py").write_text("", encoding="utf-8")
    (rogue / "bootstrap.py").write_text(
        "PINOKIO_STOP_HANDOFF_VERSION = 1\n", encoding="utf-8")

    probe = subprocess.run(
        [installed.python, "-I", "-c",
         "import importlib; print(importlib.import_module('astrid_runtime.bootstrap').__file__)"],
        check=True, capture_output=True, text=True,
    )
    assert Path(probe.stdout.strip()) == rogue / "bootstrap.py"

    with pytest.raises(subprocess.CalledProcessError) as raised:
        lifecycle.validate(installed.selected)
    assert "cannot safely hand off Pinokio Stop" in raised.value.stderr


def test_selected_interpreter_qualifies_encoded_and_symlinked_checkout_paths(tmp_path):
    """PEP 610 URLs are percent-encoded; profiles may name a symlinked folder."""
    spaced = tmp_path / "Program Files %20"
    spaced.mkdir()
    installed = _install_test_runtime(spaced, "selected")
    lifecycle, _calls = _qualification_lifecycle(spaced, installed.python)
    lifecycle.validate(installed.selected)

    alias = tmp_path / "alias"
    alias.symlink_to(spaced, target_is_directory=True)
    profile = json.loads(installed.profile.read_text(encoding="utf-8"))
    profile["runtime_checkout"] = str(alias / installed.source.name)
    write_json(installed.profile, profile)
    lifecycle.validate(installed.selected)


def test_selected_interpreter_refuses_source_path_imports_without_installed_package(tmp_path):
    """A .pth import of the recorded checkout is not the Backend.stage layout."""
    installed = _install_test_runtime(tmp_path, "selected")
    lifecycle, _calls = _qualification_lifecycle(tmp_path, installed.python)
    for item in installed.site.iterdir():
        if item.name.startswith("astrid_runtime"):
            shutil.rmtree(item)
    (installed.site / "runtime-checkout.pth").write_text(str(installed.source) + "\n",
                                                         encoding="utf-8")
    probe = subprocess.run(
        [installed.python, "-I", "-c",
         "import importlib; print(importlib.import_module('astrid_runtime.bootstrap').__file__)"],
        check=True, capture_output=True, text=True,
    )
    assert Path(probe.stdout.strip()).resolve().parent.parent == installed.source.resolve()

    with pytest.raises(subprocess.CalledProcessError) as raised:
        lifecycle.validate(installed.selected)
    assert "cannot safely hand off Pinokio Stop" in raised.value.stderr


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


@pytest.mark.parametrize("rollback", [False, True])
def test_real_journal_and_installer_log_reach_doctor(installed, monkeypatch, capsys, rollback):
    from astrid.installer.backend import Backend
    from astrid_runtime import cli
    from astrid_runtime.paths import RuntimePaths, _install_record_path
    from runtime_protocol import platform

    env = installed
    paths = RuntimePaths.sandbox(env.tmp_path / "home")
    selection = _install_record_path(paths.home)
    selection.parent.mkdir(parents=True, exist_ok=True)
    selection.write_text(json.dumps({"owner": LABEL, "program_root": str(env.root), "data_root": str(env.data)}))
    env.backend.log = env.tmp_path / "actual-installer.log"
    secret_lines = ["Authorization: Bearer bearer-secret", "Cookie: session=cookie-secret",
                    '"api_key": "api-secret"', "token=token-secret", str(paths.home / "private")]
    Backend.run(env.backend, [sys.executable, "-c", "print(" + repr("\n".join(secret_lines)) + ")"])
    assert platform.is_owner_only(env.backend.log)
    if rollback:
        start = env.lifecycle.start
        def fail_candidate(version, *args):
            if version == env.candidate:
                raise ValueError("candidate failed")
            return start(version, *args)
        env.lifecycle.start = fail_candidate
        with pytest.raises(ValueError, match="candidate failed"):
            run(env)
    else:
        run(env)
    journal = read_json(env.root / "update-transaction.json")
    assert journal["log_path"] == str(env.backend.log)
    monkeypatch.setattr(cli, "_paths", lambda _args: paths)
    assert cli.main(["doctor"]) == 1
    report = capsys.readouterr().out
    assert "Last update result: " + ("rolled_back" if rollback else "complete") in report
    assert "Installer transaction log:" in report and "[REDACTED]" in report
    assert "catalog_missing" in report
    for secret in ["bearer-secret", "cookie-secret", "api-secret", "token-secret", str(paths.home)]:
        assert secret not in report


@pytest.mark.parametrize("mode", ["migration", "app", "repair"])
def test_admission_is_durable_before_staged_and_stop(installed, monkeypatch, mode):
    from runtime_protocol import platform

    env = installed
    replaced = []
    publication_states = []
    replace = platform.durable_replace

    def publish(source, target):
        if target != env.root / "run_runtime.py":
            return replace(source, target)
        assert env.owner["running"]
        publication_states.append(tuple(env.journal_steps))
        replace(source, target)
        replaced.append(target)

    monkeypatch.setattr(platform, "durable_replace", publish)
    real_stop = env.lifecycle.stop

    def stop(*args):
        assert replaced
        assert ("staged",) in publication_states
        assert env.journal_steps[-1] == "stopping"
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
        message = ("Another Astrid operation" if boundary == "published" else
                   "An Astrid update was interrupted; Choose Pinokio's Update button")
        with pytest.raises(ValueError, match=message):
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


def test_pinokio_update_refuses_older_served_release_with_pinokio_action(installed):
    installed.backend.register = lambda *_args: None
    installed.backend.sync_skills = lambda *_args: None
    with pytest.raises(ValueError) as caught:
        update.update_pinokio(
            installed.root, installed.backend, {"release": 10}, lifecycle=installed.lifecycle,
            say=lambda _message: None,
        )

    assert str(caught.value) == (
        "This Astrid is newer than astrid.haus offers. Choose Pinokio's Update button "
        "when a newer release is available."
    )
    assert installed.calls == []
    assert installed.journal_steps == []
    assert current(installed.root) == installed.old
    assert installed.candidate_admissions == []


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
    details = {"safe": False, "kind": "busy", "blockers": [{"task_id": "task-7", "state": "running"}]}
    failure = RuntimeOperationError("Astrid is busy", details=details)
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
    assert installed.lifecycle.stop.call_count == 1
    assert raised.value.details == details
    from astrid.installer.core import is_lifecycle_refusal
    assert is_lifecycle_refusal(raised.value)
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

    from astrid.installer.core import is_lifecycle_refusal
    assert not is_lifecycle_refusal(raised.value)
    assert current(installed.root) == installed.old
    assert installed.calls == ["stage", "stop:old", "start:new", "stop:new", "start:old"]


@pytest.mark.parametrize("category", ["busy", "timeout", "identity"])
def test_initial_stop_failure_uses_only_authentic_lifecycle_refusal(installed, category):
    if category == "busy":
        details = {"safe": False, "kind": "busy", "blockers": [{"task_id": "task-1"}]}
        failure = RuntimeOperationError(
            "Astrid is busy (1 tasks running). Try again when they finish.", details=details,
        )
    elif category == "timeout":
        details = {"kind": "transport", "reason": "timed out"}
        failure = RuntimeOperationError("Runtime stop timed out", details=details)
    else:
        details = {"kind": "identity", "expected": {"instance_id": "old"}}
        failure = RuntimeOperationError("Runtime owner identity changed", details=details)

    installed.lifecycle.stop = Mock(side_effect=failure)
    installed.lifecycle.running = Mock(side_effect=AssertionError("must not probe after refusal"))
    installed.lifecycle.running.assert_not_called()
    assert current(installed.root) == installed.old
    assert installed.record["identity"] == installed.old.document()["identity"]
    assert installed.owner == {"version": "old", "running": True}
    if category == "busy":
        with pytest.raises(RuntimeOperationError) as raised:
            run(installed)
        assert raised.value is failure
        assert raised.value.details == details
        assert installed.lifecycle.stop.call_count == 1
        assert installed.calls == ["stage"]
        assert installed.journal_steps[-1] == "complete"
        assert read_json(installed.root / "update-transaction.json")["outcome"] == "stop_refused"
    else:
        with pytest.raises(RuntimeError, match="recovery also failed") as raised:
            run(installed)
        assert raised.value.__cause__ is failure
        assert str(failure) in str(raised.value)
        assert installed.lifecycle.stop.call_count == 2
        assert installed.calls == ["stage"]
        journal = read_json(installed.root / "update-transaction.json")
        assert journal["step"] == "stopping"
        assert "outcome" not in journal


def test_controller_cleanup_failure_after_down_restarts_old(installed):
    stop_calls = []

    def stop(version, *_):
        stop_calls.append(version)
        if len(stop_calls) == 1:
            installed.owner.update(version=version.root.name, running=False)
            raise RuntimeOperationError("controller cleanup")
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
            raise RuntimeOperationError("Astrid is busy during rollback",
                                        details={"safe": False, "task_count": 1})

    installed.lifecycle.start = start
    installed.lifecycle.stop = stop

    with pytest.raises(RuntimeOperationError, match="busy during rollback") as raised:
        run(installed)

    assert raised.value.details == {"safe": False, "task_count": 1}
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
    assert "ASTRID_INSTALLER_OPERATION_ID" not in args[0][3]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == update.subprocess.DEVNULL
    assert "ASTRID_INSTALLER_OPERATION_ID" not in kwargs["env"]
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
    version = Version(
        env.old.root, env.old.python, env.old.profile,
        {**env.old.env, "ASTRID_RUNTIME_DOOR_PORT": "19991"},
        env.old.release, "pinokio",
    )
    status = {
        "health": {
            "status": "ok", "runtime_instance_id": "instance-local",
            "identity": {"release": version.release, "managed_by": version.managed_by},
            "services": [{"name": "pack-host", "state": "running"}],
        },
        "discovery": {"runtime_instance_id": "instance-local"},
        "support": {"pid_alive": True},
    }
    raw_health = dict(status["health"])
    status["health"] = {key: value for key, value in status["health"].items() if key != "identity"}
    backend.runtime = lambda *_args, **_kwargs: status
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary.owner_startup_identity",
                        lambda _self, **_kwargs: raw_health)
    runtime = env.data / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    write_json(runtime / "discovery.json", {
        "active_realm": "realm", "runtime_instance_id": "instance-local",
        "identity": {"release": version.release, "managed_by": version.managed_by},
    })
    write_json(runtime / "instance.lock", {
        "runtime_instance_id": "instance-local", "realm_id": "realm",
    })
    (runtime / "generic-host.ready.json").write_text("{}", encoding="utf-8")
    opener = SimpleNamespace(open=lambda *_args, **_kwargs: pytest.fail(
        "Local verification must not contact the hosted browser door"))
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: opener)

    backend.verify(version, env.data, "realm", timeout=1, hosted=False)


@pytest.mark.parametrize("source", ["health", "discovery"])
@pytest.mark.parametrize("field", ["release", "managed_by"])
def test_local_verification_rejects_mismatched_managed_version_identity(
        installed, monkeypatch, source, field):
    from astrid.installer.backend import Backend

    env = installed
    backend = Backend(env.root, env.tmp_path / "install.log", env.tmp_path / "uv", managed_by="pinokio")
    version = env.old
    identity = {"release": version.release, "managed_by": version.managed_by}
    wrong_identity = dict(identity)
    wrong_identity[field] = (identity[field] + 1 if field == "release"
                             else ("pinokio" if identity[field] is None else None))
    health_identity = wrong_identity if source == "health" else identity
    discovery_identity = wrong_identity if source == "discovery" else identity
    status = {
        "health": {
            "status": "ok", "runtime_instance_id": "instance-local",
            "identity": health_identity,
            "services": [{"name": "pack-host", "state": "running"}],
        },
        "discovery": {"runtime_instance_id": "instance-local"},
        "support": {"pid_alive": True},
    }
    raw_health = dict(status["health"])
    status["health"] = {key: value for key, value in status["health"].items() if key != "identity"}
    backend.runtime = lambda *_args, **_kwargs: status
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary.owner_startup_identity",
                        lambda _self, **_kwargs: raw_health)
    runtime = env.data / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    write_json(runtime / "discovery.json", {
        "active_realm": "realm", "runtime_instance_id": "instance-local",
        "identity": discovery_identity,
    })
    write_json(runtime / "instance.lock", {
        "runtime_instance_id": "instance-local", "realm_id": "realm",
    })
    (runtime / "generic-host.ready.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: SimpleNamespace(
        open=lambda *_a, **_k: pytest.fail("Local verification must not contact the hosted browser door")))

    with pytest.raises(VerificationError, match="different Runtime, release|identity"):
        backend.verify(version, env.data, "realm", timeout=0.02, hosted=False)


@pytest.mark.parametrize("owner_health", [
    None,
    {"status": "ok", "runtime_instance_id": "instance-local",
     "services": [{"name": "pack-host", "state": "running"}]},
    {"status": "ok", "runtime_instance_id": "foreign-instance",
     "identity": {"release": 1, "managed_by": "pinokio"},
     "services": [{"name": "pack-host", "state": "running"}]},
])
def test_local_verification_rejects_missing_raw_managed_identity(installed, monkeypatch, owner_health):
    from astrid.installer.backend import Backend

    env = installed
    backend = Backend(env.root, env.tmp_path / "install.log", env.tmp_path / "uv", managed_by="pinokio")
    version = env.old
    status = {
        "health": {"status": "ok", "runtime_instance_id": "instance-local",
                   "services": [{"name": "pack-host", "state": "running"}]},
        "discovery": {"runtime_instance_id": "instance-local"},
        "support": {"pid_alive": True},
    }
    backend.runtime = lambda *_args, **_kwargs: status
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary.owner_startup_identity",
                        lambda _self, **_kwargs: owner_health)
    runtime = env.data / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    write_json(runtime / "discovery.json", {
        "active_realm": "realm", "runtime_instance_id": "instance-local",
        "identity": {"release": version.release, "managed_by": version.managed_by},
    })
    write_json(runtime / "instance.lock", {
        "runtime_instance_id": "instance-local", "realm_id": "realm",
    })
    (runtime / "generic-host.ready.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: SimpleNamespace(
        open=lambda *_a, **_k: pytest.fail("Local verification must not contact the hosted browser door")))

    with pytest.raises(VerificationError, match="different Runtime|identity|owner health"):
        backend.verify(version, env.data, "realm", timeout=0.02, hosted=False)


def test_local_verification_rejects_mismatched_transaction_binding(installed, monkeypatch):
    from astrid.installer.backend import Backend

    env = installed
    backend = Backend(env.root, env.tmp_path / "install.log", env.tmp_path / "uv", managed_by="pinokio")
    version = env.old
    status = {
        "health": {
            "status": "ok", "runtime_instance_id": "instance-local",
            "services": [{"name": "pack-host", "state": "running"}],
        },
        "discovery": {"runtime_instance_id": "instance-local"},
        "support": {"pid_alive": True},
    }
    raw_health = dict(status["health"])
    status["health"] = {key: value for key, value in status["health"].items() if key != "identity"}
    backend.runtime = lambda *_args, **_kwargs: status
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary.owner_startup_identity",
                        lambda _self, **_kwargs: raw_health)
    runtime = env.data / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    write_json(runtime / "discovery.json", {
        "active_realm": "realm", "runtime_instance_id": "different-instance",
    })
    write_json(runtime / "instance.lock", {
        "runtime_instance_id": "different-instance", "realm_id": "realm",
    })
    (runtime / "generic-host.ready.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: SimpleNamespace(
        open=lambda *_a, **_k: pytest.fail("Local verification must not contact the hosted browser door")))

    with pytest.raises(VerificationError, match="different Runtime|identity"):
        backend.verify(version, env.data, "realm", timeout=0.02, hosted=False)


def test_cli_requires_explicit_pinokio_mode():
    from astrid.installer.cli import main

    with pytest.raises(SystemExit):
        main(["--update-app"])


@pytest.fixture
def stop_controller(installed):
    """A real loopback controller that records each authenticated request."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    requests = []
    reply = {"status": 200, "body": {"ok": True}}
    record = installed.root / "pinokio-control.json"
    token = "a" * 64

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer " + token
            assert self.headers["Host"] == f"127.0.0.1:{self.server.server_port}"
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            assert body == b'{"c1":"stopped"}'  # byte-exact data, never a command
            requests.append((self.path, json.loads(body or b"null")))
            if reply["status"] == 200:
                record.unlink()
            self.send_response(reply["status"])
            self.end_headers()
            self.wfile.write(json.dumps(reply["body"]).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    write_json(record, {"schema": 1, "pid": os.getpid(), "port": server.server_port, "token": token})
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield requests, record, reply
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


STOPPED = {"status": "stopped"}  # C1's completed-stop result


def _bare_lifecycle(installed, stop):
    lifecycle = object.__new__(update.PinokioLifecycle)
    lifecycle.root = installed.root
    lifecycle.backend = SimpleNamespace(stop=stop)
    return lifecycle


def _no_signal(monkeypatch):
    sent = []
    for name in ('kill', 'killpg', 'terminate'):
        if hasattr(update.os, name):
            monkeypatch.setattr(update.os, name, lambda *a, _n=name: sent.append((_n, a)))
    return sent


class _Refused:
    """Connection whose POST transport fails, as when a controller just exited."""
    posts = []

    def __init__(self, *_a, **_k): pass
    def request(self, method, path, *_a, **_k): _Refused.posts.append((method, path))
    def getresponse(self): raise ConnectionRefusedError('refused')
    def close(self): pass


@pytest.mark.parametrize("failure", [None, "busy", "timeout", "identity"])
def test_runtime_handoff_precedes_controller_cleanup(installed, stop_controller, failure):
    requests, record, _reply = stop_controller
    calls = []

    def c1(version, data):
        # C1 runs with no controller request (or probe) before it.
        assert requests == []
        assert (version, data) == (installed.old, installed.data)
        calls.append("C1")
        if not failure:
            return STOPPED
        if failure:
            details = ({"safe": False, "kind": "busy"} if failure == "busy"
                       else {"kind": failure, "reason": failure})
            raise RuntimeOperationError(f"Runtime stop {failure}", details=details)

    lifecycle = _bare_lifecycle(installed, c1)
    if failure:
        with pytest.raises(RuntimeOperationError, match=f"Runtime stop {failure}") as raised:
            lifecycle.stop(installed.old, installed.data)
        from astrid.installer.core import is_lifecycle_refusal
        assert is_lifecycle_refusal(raised.value) is (failure == "busy")
        if failure != "busy":
            assert raised.value.details == {"kind": failure, "reason": failure}
        assert requests == []
        assert record.exists()
    else:
        lifecycle.stop(installed.old, installed.data)
        # C1's result is reported as data on the ordinary /stop route.
        assert requests == [("/stop", {"c1": "stopped"})]
        assert not record.exists()
    assert calls == ["C1"]


def test_pinokio_controller_failure_reports_completed_runtime_stop(installed, stop_controller):
    requests, record, reply = stop_controller
    reply.update(status=500, body={"ok": False, "errors": ["controller cleanup failed"],
                                   "details": {"safe": False, "blockers": ["late"]}})
    lifecycle = _bare_lifecycle(installed, lambda _version, _data: STOPPED)
    with pytest.raises(RuntimeOperationError, match="controller cleanup failed") as raised:
        lifecycle.stop(installed.old, installed.data)
    from astrid.installer.core import is_lifecycle_refusal
    assert not is_lifecycle_refusal(raised.value)
    assert raised.value.details == {"cause_details": {"safe": False, "blockers": ["late"]}}
    assert requests == [("/stop", {"c1": "stopped"})]
    assert record.exists()


@pytest.mark.parametrize("controller", ["exited", "remaining", "replacement"])
def test_controller_exit_during_authenticated_stop_preserves_other_owners(installed, monkeypatch, controller):
    sent = _no_signal(monkeypatch)
    record = installed.root / "pinokio-control.json"
    write_json(record, {"schema": 1, "pid": 4321, "port": 1, "token": "b" * 64})
    monkeypatch.setattr(update.runtime_platform, "process_identity", lambda pid: {"pid": pid, "birth_id": "live"})
    _Refused.posts = []

    class Connection(_Refused):
        def getresponse(self):
            if controller == "exited":
                record.unlink()
            elif controller == "replacement":
                write_json(record, {"schema": 1, "pid": 4322, "port": 2, "token": "c" * 64})
            raise ConnectionRefusedError("refused")

    monkeypatch.setattr(update.http.client, "HTTPConnection", Connection)
    lifecycle = _bare_lifecycle(installed, lambda _version, _data: STOPPED)
    if controller == "exited":
        lifecycle.stop(installed.old, installed.data)
        assert not record.exists()
    else:
        with pytest.raises(RuntimeOperationError):
            lifecycle.stop(installed.old, installed.data)
        assert record.exists()
        if controller == "replacement":
            # The re-appearing different controller record is preserved, not adopted.
            assert json.loads(record.read_text())["pid"] == 4322
    assert _Refused.posts == [("POST", "/stop")]
    assert sent == []


def test_absent_controller_replacement_refuses_after_c1(installed, monkeypatch):
    sent = _no_signal(monkeypatch)
    record = installed.root / 'pinokio-control.json'
    _Refused.posts = []
    monkeypatch.setattr(update.http.client, "HTTPConnection", _Refused)

    def c1(_version, _data):
        write_json(record, {'schema': 1, 'pid': os.getpid(), 'port': 1, 'token': 'b' * 64})
        return STOPPED

    with pytest.raises(RuntimeOperationError, match='controller changed'):
        _bare_lifecycle(installed, c1).stop(installed.old, installed.data)
    assert record.exists()
    assert _Refused.posts == [] and sent == []


def test_dead_bound_controller_needs_only_c1(installed, monkeypatch):
    sent = _no_signal(monkeypatch)
    record = installed.root / 'pinokio-control.json'
    write_json(record, {'schema': 1, 'pid': 123, 'port': 1, 'token': 'b' * 64})
    monkeypatch.setattr(update.runtime_platform, 'process_identity', lambda *_args: None)
    _Refused.posts = []
    monkeypatch.setattr(update.http.client, "HTTPConnection", _Refused)
    calls = []
    _bare_lifecycle(installed, lambda *_a: calls.append('C1') or STOPPED).stop(installed.old, installed.data)
    assert calls == ['C1']
    assert sent == []
    assert record.exists()  # The canonical launcher removes a dead controller's record.


@pytest.mark.parametrize("observation", ["live", "absent", "unknown", "reappearing"])
def test_controller_observation_is_read_only_after_c1_and_fail_closed(installed, monkeypatch, observation):
    """Windows-style observer injected: nothing observes or signals before C1."""
    sent = _no_signal(monkeypatch)
    record = installed.root / 'pinokio-control.json'
    write_json(record, {'schema': 1, 'pid': 4321, 'port': 1, 'token': 'b' * 64})
    seen = []

    def observer(pid):
        seen.append(pid)
        if observation == 'live':
            return {'pid': pid, 'birth_id': 'win:1'}
        if observation == 'unknown':
            raise OSError('OpenProcess denied')
        if observation == 'reappearing':
            return None if len(seen) == 1 else {'pid': pid, 'birth_id': 'win:2'}
        return None

    clock = iter(range(0, 10_000))
    monkeypatch.setattr(update.runtime_platform, 'process_identity', observer)
    monkeypatch.setattr(update.time, 'sleep', lambda _d: None)
    monkeypatch.setattr(update.time, 'monotonic', lambda: float(next(clock)))
    _Refused.posts = []
    monkeypatch.setattr(update.http.client, 'HTTPConnection', _Refused)
    c1 = []

    def stop(_version, _data):
        assert seen == [] and _Refused.posts == [] and sent == []
        c1.append('C1')
        return STOPPED

    lifecycle = _bare_lifecycle(installed, stop)
    if observation == 'absent':
        lifecycle.stop(installed.old, installed.data)
    else:
        # Live and reappearing never finish; unknown is not dead.
        with pytest.raises(RuntimeOperationError):
            lifecycle.stop(installed.old, installed.data)
    assert c1 == ['C1']
    assert sent == []
    assert set(seen) == {4321}
    assert _Refused.posts == [('POST', '/stop')]
    assert record.exists()


@pytest.mark.parametrize("refusal", ["busy", "replacement", "preflight"])
def test_c1_refusal_causes_no_controller_request_or_signal(installed, monkeypatch, refusal):
    sent = _no_signal(monkeypatch)
    record = installed.root / 'pinokio-control.json'
    write_json(record, {'schema': 1, 'pid': 4321, 'port': 1, 'token': 'b' * 64})
    observed = []
    monkeypatch.setattr(update.runtime_platform, 'process_identity', lambda pid: observed.append(pid))
    _Refused.posts = []
    monkeypatch.setattr(update.http.client, 'HTTPConnection', _Refused)

    def stop(*_a, **_k):
        if refusal == "preflight":
            raise RuntimeOperationError('Runtime preflight refused', details={'safe': False, 'kind': 'preflight'})
        raise RuntimeOperationError(refusal, details={'safe': False, 'kind': refusal})

    with pytest.raises(RuntimeOperationError):
        _bare_lifecycle(installed, stop).stop(installed.old, installed.data)
    assert _Refused.posts == []
    assert observed == []
    assert sent == []
    assert record.exists()


def test_controller_ignoring_report_fails_downstream_without_cleanup(installed, stop_controller):
    """An older controller ignores the body and its own down refuses: no cleanup."""
    requests, record, reply = stop_controller
    reply.update(status=500, body={"ok": False, "errors": ["Runtime interruption refused: owner lock has no matching discovery."]})
    with pytest.raises(RuntimeOperationError, match="no matching discovery") as raised:
        _bare_lifecycle(installed, lambda _version, _data: STOPPED).stop(installed.old, installed.data)
    from astrid.installer.core import is_lifecycle_refusal
    assert not is_lifecycle_refusal(raised.value)  # C1 already ran: downstream
    assert requests == [("/stop", {"c1": "stopped"})]
    assert record.exists()


@pytest.mark.parametrize("unknown", ["raises", "timeout", "non_dict", "status_other", "downstream"])
def test_c1_unknown_result_sends_no_report(installed, monkeypatch, unknown):
    sent = _no_signal(monkeypatch)
    record = installed.root / 'pinokio-control.json'
    write_json(record, {'schema': 1, 'pid': 4321, 'port': 1, 'token': 'b' * 64})
    observed = []
    monkeypatch.setattr(update.runtime_platform, 'process_identity', lambda pid: observed.append(pid))
    _Refused.posts = []
    monkeypatch.setattr(update.http.client, 'HTTPConnection', _Refused)

    def stop(*_a, **_k):
        if unknown == "raises":
            raise RuntimeError("unexpected")
        if unknown == "timeout":
            raise TimeoutError("stop timeout")
        if unknown == "downstream":
            raise RuntimeOperationError("bookkeeping failed", details={"cause_details": None})
        return ["stopped"] if unknown == "non_dict" else {"status": "running"}

    with pytest.raises(Exception):
        _bare_lifecycle(installed, stop).stop(installed.old, installed.data)
    assert _Refused.posts == [] and observed == [] and sent == []
    assert record.exists()


def test_pinokio_validate_refuses_runtime_without_installed_distribution(installed, tmp_path, monkeypatch):
    # Older installs have .pth-only Versions with no installed astrid-runtime. The
    # qualification must refuse them with the explained message, not stop_unproven.
    env = installed
    scripts = env.root.parent.parent / "pinokio"
    scripts.mkdir()
    (scripts / "session.mjs").touch()
    (scripts / "lifecycle.mjs").touch()
    (env.root / "run").touch()
    bare = tmp_path / "bare-venv"
    venv.EnvBuilder(with_pip=False).create(bare)
    bare_python = bare / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    candidate = SimpleNamespace(root=env.candidate.root, python=bare_python, profile=env.candidate.profile)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[1:2] == ["-I"]:
            result = subprocess.run(args, capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise RuntimeError(result.stderr)
            return result.stdout
        return ""

    monkeypatch.setattr(env.backend, "run", run)
    lifecycle = update.PinokioLifecycle(env.root, env.backend)
    monkeypatch.setattr(lifecycle, "node", lambda version: env.root / "node")
    with pytest.raises(RuntimeError) as refused:
        lifecycle.validate(candidate)
    message = str(refused.value)
    assert "qualified release first" in message
    assert "stop_unproven" not in message
    assert calls[-1][1:2] == ["-I"]
