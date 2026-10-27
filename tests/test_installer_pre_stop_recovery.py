"""Public CLI recovery at durable update journal boundaries."""

import importlib
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from runtime_protocol import platform

from astrid.installer import cli, core
from astrid.installer.backend import Backend
from astrid.installer.service import LABEL
from tests.test_installer_lifecycle import _installer_runtime, _publish_owner


@pytest.mark.parametrize("refusal", [None, "busy", "unknown-owner", "replacement"])
@pytest.mark.parametrize("update_flag", [False, True], ids=["install", "update"])
def test_public_cli_recovers_crash_after_stop_intent_before_c1(
        tmp_path, monkeypatch, capsys, refusal, update_flag):
    """A fresh CLI process can recover the journal before its first C1 stop."""
    backend, data, paths, realm, boundary, manager_calls = _installer_runtime(
        tmp_path, monkeypatch)
    root = backend.root
    versions = []
    for name in ("old", "candidate"):
        folder = root / "versions" / name
        version = core.Version(folder, Path(sys.executable),
                               folder / "profile.json", {}, 17)
        core.write_json(folder / "version.json", version.document())
        versions.append(version)
    old, candidate = versions
    core.switch(root, old)
    record = {"owner": LABEL, "program_root": str(root), "data_root": str(data)}
    core.write_json(root / "install.json", record)
    core.write_json(backend.service.selection, record)
    _publish_owner(paths)
    backend.has_workspace = lambda *_: True
    backend.workspace = lambda *_: {"realm_id": "realm-1", "realm_root": str(realm)}
    backend.stage = lambda *_: candidate
    backend.validate_candidate = lambda *_: None
    installer = core.Installer(root, data, backend, say=lambda _: None)

    save = installer._save_update

    class PowerLoss(BaseException):
        pass

    def save_then_crash(transaction, step, **details):
        save(transaction, step, **details)
        if step == "stopping":
            raise PowerLoss()

    installer._save_update = save_then_crash
    try:
        installer.run("update", {})
    except PowerLoss:
        pass
    else:
        raise AssertionError("crash boundary not reached")

    journal = root / "update-transaction.json"
    pending = core.read_json(journal)
    assert pending["step"] == "stopping"
    assert "stop_authority" not in pending
    assert boundary.stop_calls == 0 and manager_calls == []

    # Pin the old installation and workspace immediately before recovery
    # admission, so the test catches accidental candidate publication or data
    # mutation even if recovery returns an error.
    protected = (root / "current",)
    before = {path: path.read_bytes() for path in protected}
    assert core.current(root).root == old.root
    # Seed a user-owned row. Runtime open/close can update SQLite's physical
    # bytes and realm bookkeeping timestamps, so compare the user's data.
    with sqlite3.connect(realm / "realm.sqlite3") as connection:
        connection.execute(
            "INSERT INTO projects "
            "(id, realm_id, slug, name, metadata_json, version, created_at, updated_at) "
            "VALUES ('project-keep', 'realm-1', 'keep', 'Keep me', '{}', 1, 't', 't')"
        )
        preserved_project = connection.execute(
            "SELECT id, slug, name, metadata_json, version FROM projects WHERE id='project-keep'"
        ).fetchone()

    if refusal == "busy":
        # Use durable work observed by the real lifecycle fence, rather than
        # replacing the stop implementation or manufacturing a busy exception.
        with sqlite3.connect(realm / "realm.sqlite3") as connection:
            connection.execute(
                "INSERT INTO runs (id, capability, status, created_at, updated_at) "
                "VALUES ('run-busy', 'render', 'running', 't', 't')")
            connection.execute(
                "INSERT INTO tasks (id, run_id, capability, status, created_at, updated_at) "
                "VALUES ('task-busy', 'run-busy', 'render', 'running', 't', 't')")
    elif refusal == "unknown-owner":
        boundary.identity_error = OSError("owner identity unavailable")

    bootstrap = importlib.import_module("astrid_runtime.bootstrap")
    fence = bootstrap.interruption_fence
    replacement_publications = {}

    @contextmanager
    def replace_at_final_fence(*args, **kwargs):
        with fence(*args, **kwargs) as report:
            replacement = _publish_owner(paths, pid=778, birth="replacement-birth")
            replacement["runtime_instance_id"] = "replacement-instance"
            core.write_json(paths.discovery_path, replacement)
            core.write_json(paths.instance_lock_path, {**replacement, "realm_id": "realm-1"})
            replacement_publications.update({
                path: path.read_bytes() for path in (paths.discovery_path, paths.instance_lock_path)})
            yield report

    if refusal == "replacement":
        monkeypatch.setattr(bootstrap, "interruption_fence", replace_at_final_fence)

    def fresh_backend(*_args, **_kwargs):
        fresh = Backend(root, backend.log, backend.uv)
        fresh.service.systemd = lambda: False
        fresh.service.login_service_exists = lambda: False
        fresh.service.disable = lambda: manager_calls.append("disable")
        fresh.service.stop = lambda: manager_calls.append("stop")
        fresh.register = lambda *_args: None
        fresh.start = lambda *_args: None
        fresh.verify = lambda *_args, **_kwargs: None
        return fresh

    monkeypatch.setattr(cli, "Backend", fresh_backend)
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    args = ["--root", str(root), "--data-root", str(data),
            "--uv", str(tmp_path / "missing-uv"),
            "--manifest", str(tmp_path / "missing-manifest")]
    if update_flag:
        args.append("--update")

    # Refusal must preserve the transaction/workspace and any replacement.
    durable = (journal, root / "current", root / "install.json", backend.service.selection,
               paths.discovery_path, paths.instance_lock_path, realm / "realm.sqlite3")
    before_admission = {path: path.read_bytes() for path in durable}
    if refusal is not None:
        assert cli.main(args) == 1
        assert {
            "busy": "Astrid is busy (1 tasks running)",
            "unknown-owner": "owner identity unavailable",
            "replacement": "Runtime discovery changed before stop",
        }[refusal] in capsys.readouterr().out
        expected = {**before_admission, **replacement_publications}
        assert {path: path.read_bytes() for path in durable} == expected
        assert boundary.stop_calls == 0
        assert manager_calls == []
        assert core.read_json(journal)["step"] == "stopping"

    # Once the busy condition clears or the original published owner is
    # restored, an ordinary CLI retry must finish recovery.
    boundary.identity_error = None
    if refusal == "busy":
        with sqlite3.connect(realm / "realm.sqlite3") as connection:
            connection.execute("UPDATE tasks SET status='succeeded' WHERE id='task-busy'")
            connection.execute("UPDATE runs SET status='succeeded' WHERE id='run-busy'")
    if refusal == "replacement":
        monkeypatch.setattr(bootstrap, "interruption_fence", fence)
        _publish_owner(paths)
    result = cli.main(args)

    assert result == 0, "public CLI failed to recover pre-admission stop intent after safe retry"
    assert core.read_json(journal)["step"] in {"rolled_back", "complete"}
    assert core.current(root).root == old.root
    assert {path: path.read_bytes() for path in protected} == before
    with sqlite3.connect(f"file:{realm / 'realm.sqlite3'}?mode=ro", uri=True) as connection:
        assert connection.execute(
            "SELECT id, slug, name, metadata_json, version FROM projects WHERE id='project-keep'"
        ).fetchone() == preserved_project
    assert boundary.stop_calls == 1
    assert manager_calls == ["disable", "stop"]
