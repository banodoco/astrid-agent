"""Real SIGKILLs exercise retained-image rollback across installer/Runtime steps."""

# Integration dependencies must be checked before importing the real backend.
# ruff: noqa: E402

from __future__ import annotations

import ast
import hashlib
import os
import re
import signal
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
import venv
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.linux_native]  # SIGKILL crash evidence; needs the Runtime

from astrid_runtime.bootstrap import BootstrapConfig, SourceProfile
from astrid_runtime.paths import RuntimePaths
from runtime_protocol.store import SCHEMA_VERSION, RealmStore
from runtime_protocol.upgrade import inspect_canonical_schema

from astrid.installer import core
from astrid.installer.backend import Backend
from astrid.installer.core import (
    Installer,
    Version,
    current,
    read_json,
    recover_transaction,
    switch,
    write_json,
)


def _event(base, name):
    with (base / "events").open("a", encoding="utf-8") as stream:
        stream.write(name + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _tree_snapshot(root):
    snapshot = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("directory",)
        elif path.is_file():
            snapshot[relative] = ("file", path.read_bytes())
    return snapshot


def _kill(point, wanted):
    if point == wanted:
        os.kill(os.getpid(), signal.SIGKILL)


def _version(root, name, release=0, managed_by=None):
    folder = root / "versions" / name
    folder.mkdir(parents=True)
    version = Version(folder, Path(sys.executable), folder / "profile.json", {}, release, managed_by)
    write_json(folder / "version.json", version.document())
    return version


def _setup(base, *, previous_schema=True):
    root, data = base / "program", base / "data"
    old = _version(root, "old", release=17)
    switch(root, old)
    write_json(root / "install.json", {"owner": "haus.astrid.runtime", "data_root": str(data)})
    paths = RuntimePaths.current(data_root=data)
    paths.ensure_support_dirs()
    realm = paths.realms_dir / "realm-1"
    RealmStore.initialize(realm, realm_id="realm-1").close()
    if previous_schema:
        with sqlite3.connect(realm / "realm.sqlite3") as connection:
            connection.execute("ALTER TABLE timelines DROP COLUMN name")
            connection.execute("DROP TABLE user_app_state")  # added by schema v31
            connection.execute("UPDATE runtime_schema SET version=29 WHERE id=1")
    import astrid_runtime
    checkout = str(Path(astrid_runtime.__file__).resolve().parents[1])
    profile = SourceProfile(profile="astrid", runtime_checkout=checkout, source_checkout=checkout)
    write_json(paths.catalog_path, {
        "version": 1, "selected_realm_id": "realm-1",
        "realms": [{"realm_id": "realm-1", "display_name": "Astrid", "data_root": str(realm)}],
        "source_profiles": {"astrid": profile.as_dict()},
    })
    write_json(paths.source_profiles_dir / "astrid.json", profile.as_dict())
    credentials = paths.runtime_support / "credentials"
    credentials.mkdir(exist_ok=True)
    (credentials / "owner.token").write_text("test-owner")
    (data / "retained.txt").write_text("original user data")


class _Backend(Backend):
    """Use real copies/restore/migration with deterministic service boundaries."""

    def __init__(self, base, kill_at):
        self.base, self.root, self.kill_at = base, base / "program", kill_at
        self.log = base / "install.log"
        self.say = print
        self.managed_by = None
        self.process_control = None
        from astrid.installer.service import Service

        self.service = Service(self.root, self.log, self.run, home=base / "home", system="linux")
        self.service.selection = base / "install-selection.json"
        if not self.service.selection.exists():
            write_json(self.service.selection, {
                "owner": "haus.astrid.runtime", "program_root": str(self.root),
                "data_root": str(base / "data"), "identity": {"release": 17, "managed_by": None},
                "unrelated": "preserved",
            })

    def existing(self, _data):
        return None

    def has_workspace(self, _data):
        return True

    def reinstallable(self, _data):
        return False

    def workspace(self, _version, data):
        catalog = read_json(data / "runtime" / "catalog.json")
        realm = catalog["realms"][0]
        return {"realm_id": realm["realm_id"], "realm_root": realm["data_root"]}

    def stop(self, _version, data):
        context = getattr(self, "_update_transaction", None)
        if (getattr(self, "real_c1_recovery", False) and context is not None
                and context[1] is True and context[0].get("step") == "recovering"):
            # Retries take the production Backend.stop -> Runtime C1 path. The
            # initial crash setup below still uses this fixture's deterministic
            # service boundary.
            return Backend.stop(self, _version, data)
        _event(self.base, "stop")
        for name in ("discovery.json", "instance.lock"):
            (data / "runtime" / name).unlink(missing_ok=True)
        if _version.root.name == "old":
            _kill("stop-return", self.kill_at)

    def backup(self, _version, data, realm):
        # A new snapshot must never capture the interrupted Runtime journal.
        journal = read_json(data / "runtime" / "upgrade-journal.json")
        assert journal.get("state") in {None, "complete"}, journal
        _event(self.base, "backup")
        destination = self.root / "backups" / uuid.uuid4().hex
        destination.mkdir(parents=True)
        kind = "clone"
        if (self.base / "authenticated").exists():
            from runtime_protocol.backup import create_backup

            store = RealmStore(realm["realm_root"])
            try:
                create_backup(store, destination / "realm", key_path=data / "runtime/backup-auth.key")
            finally:
                store.close()
            kind = "runtime"
        else:
            self._copy(Path(realm["realm_root"]), destination / "realm")
        self._copy(data, destination / "support", support=True)
        result = {"root": str(destination), "kind": kind}
        write_json(destination / "backup.json", result)
        _kill("backup-return", self.kill_at)
        return result

    def stage(self, _manifest, root, _data):
        candidate = _version(root, "new-" + uuid.uuid4().hex[:8], release=18)
        _kill("stage-return", self.kill_at)
        return candidate

    def validate_candidate(self, _candidate, _data, _realm):
        pass

    def upgrade(self, _version, _data):
        result = subprocess.run([sys.executable, __file__, "runtime", str(self.base), self.kill_at],
                                capture_output=True, text=True, timeout=30)
        if result.returncode == -signal.SIGKILL:
            # A machine/process-group crash cannot enter core's exception path.
            os.kill(os.getpid(), signal.SIGKILL)
        assert result.returncode == 0, result.stderr

    def restore(self, old, data, realm, backup):
        _event(self.base, "restore")
        if self.kill_at and self.kill_at.startswith("legacy-"):
            _historical_backend_restore(self, old, data, realm, backup)
            return
        super().restore(old, data, realm, backup)
        _kill("restore-return", self.kill_at)

    def register(self, version, _data):
        record = self.service.recorded()
        record.pop("uninstalled", None)
        write_json(self.service.selection, {**record, "identity": {
            "release": version.release, "managed_by": version.managed_by}})
        _event(self.base, "register")

    def start(self, _version, _data):
        _event(self.base, "start")
        if getattr(self, "real_runtime_proof", False) and _version.root.name == "old":
            context = getattr(self, "_update_transaction", None)
            self._update_transaction = None
            try:
                return Backend.start(self, _version, _data)
            finally:
                self._update_transaction = context
        if _version.root.name.startswith("new-"):
            _kill("start-return", self.kill_at)

    def verify(self, _version, data, realm_id, **_kwargs):
        if getattr(self, "real_runtime_proof", False) and _version.root.name == "old":
            context = getattr(self, "_update_transaction", None)
            self._update_transaction = None
            try:
                deadline = time.monotonic() + 10
                status = {}
                while time.monotonic() < deadline:
                    status = Backend.runtime(self, _version, data, "status")
                    discovery = status.get("discovery") or {}
                    lock = read_json(data / "runtime/instance.lock") or {}
                    health = status.get("health") or {}
                    if (discovery.get("active_realm") == realm_id
                            and health.get("status") == "ok"
                            and health.get("runtime_instance_id") == discovery.get("runtime_instance_id")
                            and lock.get("runtime_instance_id") == discovery.get("runtime_instance_id")
                            and lock.get("realm_id") == realm_id
                            and status.get("support", {}).get("pid_alive") is True):
                        break
                    time.sleep(0.2)
                else:
                    raise AssertionError(f"Old Runtime did not verify as healthy: {status}")
            finally:
                active_exception = sys.exc_info()[0]
                try:
                    Backend.stop(self, _version, data)
                except Exception:
                    if active_exception is None:
                        raise
                finally:
                    self._update_transaction = context
        realm = self.workspace(_version, data)
        assert realm["realm_id"] == realm_id == "realm-1"
        assert inspect_canonical_schema(realm["realm_root"])["realm_id"] == realm_id
        assert (data / "retained.txt").read_text() == "original user data"
        _event(self.base, "verify-" + _version.root.name)
        _event(self.base, "verify")

    def open(self):
        _event(self.base, "open")


def _run(base, kill_at="", mode="update"):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([
        str(Path(__file__).resolve().parents[1]), os.environ.get("PYTHONPATH", ""),
    ])}
    if mode == "cli-update":
        env.update({
            "HOME": str(Path(base) / "home"),
            "XDG_DATA_HOME": str(Path(base) / "home/xdg-data"),
            "XDG_CONFIG_HOME": str(Path(base) / "home/xdg-config"),
        })
    result = subprocess.run([sys.executable, __file__, mode, str(base), kill_at],
                            env=env, capture_output=True, text=True, timeout=45)
    return result


_LEGACY_RESTORE_SOURCE = '''
def restore(self, old: Version, data: Path, realm: dict, backup: dict) -> None:
    if platform.sys.platform == "win32":
        platform.private_directory(data)
    destination = Path(backup["root"])
    realm_root = absolute(realm["realm_root"])
    restored = realm_root.with_name(realm_root.name + ".restore-" + uuid.uuid4().hex[:8])
    if backup["kind"] == "clone":
        self._copy(destination / "realm", restored, clone=True)
    else:
        script = ("from pathlib import Path; from runtime_protocol.backup import restore_backup; import sys; "
                  "key=Path(sys.argv[3]); "
                  "restore_backup(sys.argv[1],sys.argv[2],**({'key_path':key} if key.is_file() else {}))")
        retained_key = destination / "support" / "runtime" / "backup-auth.key"
        self.run([old.python, "-X", "utf8", "-I", "-c", script, destination / "realm", restored, retained_key],
                 extra_env=old.env)
    failed = realm_root.with_name(realm_root.name + ".failed-" + uuid.uuid4().hex[:8])
    if realm_root.exists():
        platform.replace_path(realm_root, failed)
    try:
        platform.replace_path(restored, realm_root)
    except BaseException:
        if failed.exists() and not realm_root.exists():
            platform.replace_path(failed, realm_root)
        raise
    for failed_copy in sorted(path for path in realm_root.parent.glob(realm_root.name + ".failed-*")
                              if path.is_dir() and not path.is_symlink()):
        self.say(f"The failed update's workspace copy is kept at {failed_copy}. You can delete it once Astrid works.")
    def copy_support(source: Path, target: Path) -> None:
        if source.is_dir():
            platform.private_directory(target)
            for entry in source.iterdir():
                copy_support(entry, target / entry.name)
            shutil.copystat(source, target)
        else:
            descriptor, temporary_name = platform.private_temporary(target.parent, ".restore-")
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                shutil.copy2(source, temporary)
                if platform.sys.platform == "win32":
                    platform.owner_only(temporary)
                with temporary.open("rb") as stream:
                    os.fsync(stream.fileno())
                platform.durable_replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
    snapshot = destination / "support"
    for item in snapshot.iterdir():
        if item.name == "logs":
            continue
        target = data / item.name
        if item.name == "runtime":
            platform.private_directory(target)
            for entry in target.iterdir():
                if entry.name != "realms":
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
            copy_support(item, target)
        else:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            copy_support(item, target)
'''
_LEGACY_RESTORE_AST_SHA256 = "fd6904b1132447e28f5f970109cdb78b8e5296a447db51189a955b3c3a63738b"


def _historical_backend_restore(self, old, data, realm, backup):
    """Run the retained pre-amendment restore source (24cc550) for crash setup."""
    method = ast.parse(_LEGACY_RESTORE_SOURCE).body[0]
    assert hashlib.sha256(ast.unparse(method).encode()).hexdigest() == _LEGACY_RESTORE_AST_SHA256
    module = ast.Module(body=[method], type_ignores=[])
    namespace = dict(vars(sys.modules["astrid.installer.backend"]))
    exec(compile(ast.fix_missing_locations(module), "24cc550:astrid/installer/backend.py", "exec"), namespace)
    namespace["restore"](self, old, data, realm, backup)


def _assert_retry(base, *, restore_expected):
    events = base / "events"
    before = events.read_text().splitlines() if events.exists() else []
    for attempt in range(3):
        result = _run(base)
        assert result.returncode == 0, result.stderr
        if attempt == 0 and restore_expected:
            match = re.search(r"The failed update's workspace copy is kept at (.+)\. You can delete it once Astrid works\.", result.stdout)
            assert match, result.stdout
            failed_copy = Path(match.group(1))
            assert failed_copy.name.startswith("realm-1.failed-")
            assert failed_copy.is_dir()
        transaction = read_json(base / "program" / "update-transaction.json")
        assert transaction["step"] == "complete"
        assert current(base / "program").root.name.startswith("new-")
        assert read_json(base / "data/runtime/upgrade-journal.json")["state"] == "complete"
        realm = base / "data/runtime/realms/realm-1"
        assert inspect_canonical_schema(realm)["version"] == SCHEMA_VERSION
    after = (base / "events").read_text().splitlines()[len(before):]
    if restore_expected:
        assert after.index("restore") < after.index("backup")
    else:
        assert "restore" not in after


@pytest.mark.parametrize("step", [
    "stage-return", "staged", "stopping", "stop-return", "stopped", "backup-return",
    "backed_up", "runtime:planned", "runtime:stopped", "runtime:migration-committed",
    "runtime:activated", "runtime:restarted", "migrated", "pointer-publish", "switched",
    "start-return", "verified", "complete",
])
def test_sigkill_each_installer_durable_step_then_three_successful_updates(tmp_path, step):
    _setup(tmp_path)
    result = _run(tmp_path, step)
    assert result.returncode == -signal.SIGKILL, result.stderr
    restore_expected = step in {
        "backed_up", "runtime:planned", "runtime:stopped", "runtime:migration-committed",
        "runtime:activated", "runtime:restarted", "migrated", "pointer-publish", "switched",
        "start-return", "verified",
    }
    _assert_retry(tmp_path, restore_expected=restore_expected)


def test_candidate_validation_failure_keeps_owner_pointer_and_workspace(tmp_path):
    _setup(tmp_path)
    root, data = tmp_path / "program", tmp_path / "data"
    old = current(root)
    before_workspace = (data / "retained.txt").read_bytes()
    backend = _Backend(tmp_path, None)
    before_record = read_json(tmp_path / "install-selection.json")

    def reject(_candidate, _data, _realm):
        raise ValueError("candidate hash mismatch")

    backend.validate_candidate = reject
    with pytest.raises(ValueError, match="candidate hash mismatch"):
        Installer(root, data, backend, say=lambda _line: None).run("update", {})

    assert not (tmp_path / "events").exists()
    assert current(root) == old
    assert read_json(tmp_path / "install-selection.json") == before_record
    assert (data / "retained.txt").read_bytes() == before_workspace


def test_recovery_stop_refusal_keeps_durable_transaction_unchanged(tmp_path):
    _setup(tmp_path)
    result = _run(tmp_path, "stopping")
    assert result.returncode == -signal.SIGKILL, result.stderr
    root, data = tmp_path / "program", tmp_path / "data"
    journal_path = root / "update-transaction.json"
    original = journal_path.read_bytes()
    backend = _Backend(tmp_path, None)
    details = {"safe": False, "blockers": [{"task_id": "task-busy", "state": "running"}]}

    def refuse(*_args):
        raise core.RuntimeOperationError("Astrid is busy (1 tasks running). Try again when they finish.",
                                         details=details)

    backend.stop = refuse
    with pytest.raises(core.RuntimeOperationError) as raised:
        Installer(root, data, backend, say=lambda _line: None)._recover_update()

    assert raised.value.details == details
    assert journal_path.read_bytes() == original


@pytest.mark.parametrize("state", [
    "planned", "stopping", "stopped", "migration-committed", "activated", "restarted", "complete",
])
def test_sigkill_actual_runtime_upgrade_then_recover_before_new_backup(tmp_path, state):
    _setup(tmp_path)
    result = _run(tmp_path, "runtime:" + state)
    assert result.returncode == -signal.SIGKILL, result.stderr
    journal = read_json(tmp_path / "data/runtime/upgrade-journal.json")
    assert journal["state"] == ("stopped" if state == "migration-committed" else state)
    transaction = read_json(tmp_path / "program/update-transaction.json")
    assert transaction["backup"]["root"]
    assert transaction["realm"]["realm_id"] == journal["realm_id"]
    if state == "migration-committed":
        assert inspect_canonical_schema(transaction["realm"]["realm_root"])["version"] == SCHEMA_VERSION
        assert inspect_canonical_schema(Path(transaction["backup"]["root"]) / "realm")["version"] == 29
    _assert_retry(tmp_path, restore_expected=True)


@pytest.mark.parametrize("point", ["restore-realm-removed", "restore-support-removed", "restore-return"])
def test_sigkill_during_recovery_replays_same_retained_backup(tmp_path, point):
    _setup(tmp_path)
    assert _run(tmp_path, "runtime:stopped").returncode == -signal.SIGKILL
    original = read_json(tmp_path / "program/update-transaction.json")
    result = _run(tmp_path, point)
    assert result.returncode == -signal.SIGKILL, result.stderr
    interrupted = read_json(tmp_path / "program/update-transaction.json")
    assert interrupted["operation_id"] == original["operation_id"]
    assert interrupted["backup"] == original["backup"]
    assert interrupted["step"] == "recovering"
    _assert_retry(tmp_path, restore_expected=True)


@pytest.mark.parametrize("point", [
    "legacy-realm-replaced", "legacy-catalog-deleted", "legacy-catalog-copy-staged",
])
def test_baseline_restore_catalog_gap_recovers_through_public_installer_and_real_c1(tmp_path, point):
    """Replay a true baseline restore crash using the real update recovery path."""
    _setup(tmp_path, previous_schema=False)
    root, data = tmp_path / "program", tmp_path / "data"
    backend = _Backend(tmp_path, None)
    old = current(root)
    # This integration case launches the retained old service through its real
    # isolated command. Give the synthetic version a private environment with
    # the checked out Agent package installed, as a prepared release would have.
    environment = old.root / "test-venv"
    venv.EnvBuilder(system_site_packages=True, with_pip=False).create(environment)
    site_packages = environment / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    shutil.copytree(Path(__file__).resolve().parents[1] / "astrid", site_packages / "astrid")
    (site_packages / "pyproject.toml").write_bytes(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_bytes())
    host_site_packages = Path(sys.prefix) / "lib" / \
        f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    (site_packages / "prepared-agent-environment.pth").write_text(
        str(host_site_packages) + "\n", encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        door_port = listener.getsockname()[1]
    old = Version(old.root, environment / "bin" / "python", old.profile,
                  {**old.env, "ASTRID_RUNTIME_DOOR_PORT": str(door_port)},
                  old.release, old.managed_by)
    write_json(old.root / "version.json", old.document())
    switch(root, old)
    import astrid_runtime
    checkout = str(Path(astrid_runtime.__file__).resolve().parents[1])
    write_json(old.profile, SourceProfile(profile="astrid", runtime_checkout=checkout,
                                         source_checkout=checkout).as_dict())
    (root / "legacy-runtime-proof").touch()
    candidate = _version(root, "candidate", release=18)
    candidate_environment = candidate.root / "test-venv"
    venv.EnvBuilder(system_site_packages=True, with_pip=False).create(candidate_environment)
    candidate_site_packages = candidate_environment / "lib" / \
        f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    shutil.copytree(Path(__file__).resolve().parents[1] / "astrid",
                    candidate_site_packages / "astrid")
    (candidate_site_packages / "pyproject.toml").write_bytes(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_bytes())
    (candidate_site_packages / "prepared-agent-environment.pth").write_text(
        str(host_site_packages) + "\n", encoding="utf-8")
    candidate = Version(candidate.root, candidate_environment / "bin" / "python",
                        candidate.profile, candidate.env, candidate.release, candidate.managed_by)
    write_json(candidate.root / "version.json", candidate.document())
    paths = RuntimePaths.current(data_root=data)
    realm = paths.realms_dir / "realm-1"

    # Retain the same image a previous installer would have captured before
    # migration. The missing live catalog is created only by the historical
    # Backend.restore body below, after its realm replacement.
    backup_root = root / "backups" / "legacy-retained"
    backup_root.mkdir(parents=True)
    backend._copy(realm, backup_root / "realm")
    backend._copy(data, backup_root / "support", support=True)
    backup = {"root": str(backup_root), "kind": "clone"}
    write_json(backup_root / "backup.json", backup)
    retained_image_before = _tree_snapshot(backup_root)
    (data / "retained.txt").write_text("damaged update data")

    # The install selection is metadata only. C1 must admit the readable realm
    # from the journal plus retained catalog, without any stop receipt/evidence.
    write_json(backend.service.selection, {
        "owner": "haus.astrid.runtime", "program_root": str(root),
        "data_root": str(data), "realm_id": "realm-1", "realm_root": str(realm),
        "identity": {"release": 17, "managed_by": None}, "unrelated": "preserved",
    })
    transaction = {
        "format": "astrid-install-update/v1", "operation_id": "c" * 32,
        "transaction_kind": "migration", "step": "recovering", "data_root": str(data),
        "realm": {"realm_id": "realm-1", "realm_root": str(realm)},
        "old": old.document(), "candidate": candidate.document(), "backup": backup,
    }
    switch(root, candidate)
    write_json(root / "update-transaction.json", transaction)

    crash = _run(tmp_path, point, mode="cli-update")
    assert crash.returncode == -signal.SIGKILL, crash.stderr
    pending = read_json(root / "update-transaction.json")
    assert pending["step"] == "recovering"
    assert pending["backup"] == backup
    assert _tree_snapshot(backup_root) == retained_image_before
    assert paths.catalog_path.exists() == (point == "legacy-realm-replaced")
    if point != "legacy-realm-replaced":
        assert inspect_canonical_schema(realm)["realm_id"] == "realm-1"

    if not paths.catalog_path.exists():
        staged = _run(tmp_path, "catalog-reconstitution-staged", mode="cli-update")
        assert staged.returncode == -signal.SIGKILL, staged.stderr
        assert not paths.catalog_path.exists()
        assert read_json(root / "update-transaction.json")["backup"] == backup
        assert _tree_snapshot(backup_root) == retained_image_before
        published = _run(tmp_path, "catalog-reconstitution-published", mode="cli-update")
        assert published.returncode == -signal.SIGKILL, published.stderr
        assert read_json(paths.catalog_path)["selected_realm_id"] == "realm-1"
        assert read_json(root / "update-transaction.json")["backup"] == backup
        assert _tree_snapshot(backup_root) == retained_image_before

    # With a catalog already present, C1 preserves its current metadata. Kill
    # the new restore immediately before its catalog replacement and prove the
    # current catalog remains complete throughout support replay.
    current_catalog = read_json(paths.catalog_path)
    current_catalog["realms"][0]["display_name"] = "Current restore metadata"
    write_json(paths.catalog_path, current_catalog)
    catalog_before_restore = paths.catalog_path.read_bytes()
    interrupted_restore = _run(tmp_path, "catalog-reconstitution-staged", mode="cli-update")
    assert interrupted_restore.returncode == -signal.SIGKILL, interrupted_restore.stderr
    assert paths.catalog_path.read_bytes() == catalog_before_restore
    assert _tree_snapshot(backup_root) == retained_image_before

    # Kill each of the next three fresh recovery attempts immediately after its
    # production restore. Each has already passed real Backend.stop/C1 and must
    # retain the same historical image. The fourth fresh CLI Update recovery
    # completes rollback and verifies the old version before returning.
    for attempt in range(3):
        result = _run(tmp_path, "restore-return", mode="cli-update")
        assert result.returncode == -signal.SIGKILL, result.stderr
        repeated = read_json(root / "update-transaction.json")
        assert repeated["step"] == "recovering"
        assert repeated["backup"] == backup
        assert _tree_snapshot(backup_root) == retained_image_before

    result = _run(tmp_path, mode="cli-update")
    assert result.returncode == 0, result.stderr
    events = (tmp_path / "events").read_text().splitlines()
    assert "verify-old" in events
    assert "backup" not in events
    assert current(root).root == old.root
    assert read_json(root / "update-transaction.json")["step"] == "rolled_back"
    assert _tree_snapshot(backup_root) == retained_image_before
    assert read_json(backend.service.selection)["identity"] == old.document()["identity"]
    assert read_json(paths.catalog_path)["selected_realm_id"] == "realm-1"
    assert (data / "retained.txt").read_text() == "original user data"


def test_repair_also_recovers_before_workspace_inspection(tmp_path):
    _setup(tmp_path)
    assert _run(tmp_path, "runtime:stopped").returncode == -signal.SIGKILL
    result = _run(tmp_path, mode="install")
    assert result.returncode == 0, result.stderr
    selected = current(tmp_path / "program")
    assert selected.root.name == "old"
    assert selected.document()["identity"] == read_json(selected.root / "version.json")["identity"]
    assert selected.document()["identity"] == {"release": 17, "managed_by": None}
    selection = read_json(tmp_path / "install-selection.json")
    assert selection["identity"] == selected.document()["identity"]
    assert selection["unrelated"] == "preserved"
    assert read_json(tmp_path / "program/update-transaction.json")["step"] == "rolled_back"
    assert not (tmp_path / "data/runtime/upgrade-journal.json").exists()


@pytest.mark.parametrize("point", [
    "staged", "stopping", "stop-return", "stopped", "backed_up",
    "runtime:planned", "migrated", "pointer-publish", "switched", "start-return", "verified",
])
def test_repair_recovers_each_installer_boundary_to_old_identity_and_data(tmp_path, point):
    _setup(tmp_path)
    result = _run(tmp_path, point)
    assert result.returncode == -signal.SIGKILL, result.stderr

    result = _run(tmp_path, mode="install")
    assert result.returncode == 0, result.stderr
    root = tmp_path / "program"
    selected = current(root)
    assert selected.root.name == "old"
    assert selected.document()["identity"] == {"release": 17, "managed_by": None}
    assert read_json(tmp_path / "install-selection.json")["identity"] == selected.document()["identity"]
    assert read_json(tmp_path / "install-selection.json")["unrelated"] == "preserved"
    assert (tmp_path / "data/retained.txt").read_text() == "original user data"
    assert read_json(root / "update-transaction.json")["step"] in {"complete", "rolled_back"}


@pytest.mark.parametrize("point", ["restore-realm-removed", "restore-key-removed", "restore-return"])
def test_authenticated_runtime_backup_recovers_even_if_live_key_was_removed(tmp_path, point):
    _setup(tmp_path, previous_schema=False)
    (tmp_path / "authenticated").touch()
    assert _run(tmp_path, "runtime:stopped").returncode == -signal.SIGKILL
    assert read_json(tmp_path / "program/update-transaction.json")["backup"]["kind"] == "runtime"
    result = _run(tmp_path, point)
    assert result.returncode == -signal.SIGKILL, result.stderr
    if point == "restore-key-removed":
        assert not (tmp_path / "data/runtime/backup-auth.key").exists()
    _assert_retry(tmp_path, restore_expected=True)


@pytest.mark.parametrize("step", ["staged", "started"])
def test_app_transaction_recovery_never_mutates_workspace(tmp_path, step):
    """App-only recovery preserves the old owner without restoring workspace data."""
    root, data = tmp_path / "program", tmp_path / "data"
    old = _version(root, "old", release=17)
    candidate = _version(root, "candidate", release=18)
    workspace = data / "workspace.json"
    workspace.parent.mkdir(parents=True)
    workspace.write_text('{"realm":"kept","payload":"original"}', encoding="utf-8")
    original_workspace = workspace.read_bytes()
    switch(root, candidate if step == "started" else old)
    write_json(root / "install.json", {"owner": "haus.astrid.runtime", "data_root": str(data)})
    realm = {"realm_id": "realm-1", "realm_root": str(data)}
    write_json(root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": uuid.uuid4().hex,
        "transaction_kind": "app", "step": step, "data_root": str(data),
        "old": old.document(), "candidate": candidate.document(),
        "realm": realm, "backup": None,
    })
    events = []

    class NoMigrationBackend:
        def restore(self, *_args):
            raise AssertionError("app-only recovery must never restore workspace data")

    running = {"version": "old" if step == "staged" else "candidate"}

    def stop(version, _data):
        events.append("stop:" + version.root.name)
        running["version"] = None

    def switch_old(version, _candidate):
        events.append("switch-old")
        switch(root, version)

    def activate_old(version, _data, _realm):
        events.append("start-old")
        running["version"] = version.root.name

    recover_transaction(root, data, NoMigrationBackend(), stop=stop,
                        switch_old=switch_old, activate_old=activate_old,
                        restore_workspace=False, say=lambda _line: None)

    assert workspace.read_bytes() == original_workspace
    assert running["version"] == "old"
    if step == "staged":
        assert current(root) == old
        assert events == []
        assert read_json(root / "update-transaction.json")["step"] == "complete"
    else:
        assert current(root) == old
        assert events == ["stop:candidate", "switch-old", "start-old"]
        assert read_json(root / "update-transaction.json")["step"] == "rolled_back"


def test_legacy_staged_migration_journal_with_backup_restores_workspace(tmp_path):
    """Old v1 staged records were published after stop and backup."""
    root, data = tmp_path / "program", tmp_path / "data"
    old = _version(root, "old", release=17)
    candidate = _version(root, "candidate", release=18)
    switch(root, candidate)
    write_json(root / "install.json", {"owner": "haus.astrid.runtime", "data_root": str(data)})
    backup_root = root / "backups" / "legacy-operation"
    backup_root.mkdir(parents=True)
    current_workspace = data / "workspace.json"
    current_workspace.parent.mkdir(parents=True)
    current_workspace.write_text("migrated", encoding="utf-8")
    realm = {"realm_id": "realm-1", "realm_root": str(data)}
    backup = {"root": str(backup_root), "kind": "clone"}
    # `transaction_kind` did not exist in the old v1 layout, and staged was
    # only written after the old installer had already stopped and backed up.
    write_json(root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": uuid.uuid4().hex,
        "step": "staged", "data_root": str(data), "old": old.document(),
        "candidate": candidate.document(), "realm": realm, "backup": backup,
    })
    events = []

    class LegacyBackend:
        def restore(self, previous, _data, _realm, image):
            assert previous == old
            assert image == backup
            events.append("restore")
            current_workspace.write_text("original", encoding="utf-8")

    recover_transaction(
        root, data, LegacyBackend(),
        stop=lambda version, _data: events.append("stop:" + version.root.name),
        switch_old=lambda version, _candidate: switch(root, version),
        activate_old=lambda version, _data, _realm: events.append("start:" + version.root.name),
        say=lambda _line: None,
    )

    assert events == ["stop:candidate", "restore", "start:old"]
    assert current(root) == old
    assert current_workspace.read_text(encoding="utf-8") == "original"
    assert read_json(root / "update-transaction.json")["step"] == "rolled_back"


def _runtime(base, kill_at):
    from types import SimpleNamespace

    from astrid_runtime import operator_upgrade

    paths = RuntimePaths.current(data_root=base / "data")
    realm = paths.realms_dir / "realm-1"

    def publish():
        discovery = {
            "pid": os.getpid(), "active_realm": "realm-1", "endpoint": "http://127.0.0.1:17540",
            "runtime_instance_id": "test-instance", "process_birth_id": "test-birth", "realm_root": str(realm),
        }
        write_json(paths.discovery_path, discovery)
        write_json(paths.instance_lock_path, {**discovery, "realm_id": "realm-1"})
        return discovery

    class Boundary:
        def is_pid_alive(self, _pid): return True
        def validate_owner(self, **_kwargs): return True
        def prepare_restart(self, **_kwargs): pass
        def stop_owner(self, **_kwargs): pass
        def endpoint_metadata(self, **_kwargs):
            return {"runtime_instance_id": "test-instance", "realm_id": "realm-1"}
        def health(self, **_kwargs): return True
        def connect(self, **_kwargs): return SimpleNamespace(doctor=lambda: {"ok": True})

    def start(*_args):
        discovery = publish()
        return SimpleNamespace(endpoint=discovery["endpoint"], pid=os.getpid(), runtime_instance_id="test-instance")

    journal = operator_upgrade._journal
    migrate = operator_upgrade.migrate_canonical_to_current

    def journal_and_kill(*args, **kwargs):
        result = journal(*args, **kwargs)
        _kill("runtime:" + kwargs["state"], kill_at)
        return result

    def migrate_and_kill(*args, **kwargs):
        result = migrate(*args, **kwargs)
        _kill("runtime:migration-committed", kill_at)
        return result

    publish()
    operator_upgrade._bootstrap_locked = start
    operator_upgrade._journal = journal_and_kill
    operator_upgrade.migrate_canonical_to_current = migrate_and_kill
    operator_upgrade.upgrade_workspace(paths, Boundary(), BootstrapConfig())


def _installer(base, kill_at, mode):
    from astrid.installer import backend as backend_module
    from runtime_protocol import platform

    root, data = base / "program", base / "data"
    save = Installer._save_update
    publish = core.switch
    replace = backend_module.os.replace
    rmtree = backend_module.shutil.rmtree
    unlink = Path.unlink
    replace_path = platform.replace_path
    durable_replace = platform.durable_replace
    copy2 = backend_module.shutil.copy2

    def save_and_kill(self, transaction, step, **details):
        save(self, transaction, step, **details)
        _kill(step, kill_at)

    def switch_and_kill(*args):
        publish(*args)
        _kill("pointer-publish", kill_at)

    def replace_and_kill(source, target, *args, **kwargs):
        replace(source, target, *args, **kwargs)
        if Path(source) == data / "runtime/realms/realm-1":
            _kill("restore-realm-removed", kill_at)

    def rmtree_and_kill(path, *args, **kwargs):
        rmtree(path, *args, **kwargs)
        if Path(path) == data / "runtime/credentials":
            _kill("restore-support-removed", kill_at)

    def unlink_and_kill(path, *args, **kwargs):
        unlink(path, *args, **kwargs)
        if path == data / "runtime/backup-auth.key":
            _kill("restore-key-removed", kill_at)
        if path == data / "runtime/catalog.json":
            _kill("legacy-catalog-deleted", kill_at)

    def replace_path_and_kill(source, target, *args, **kwargs):
        result = replace_path(source, target, *args, **kwargs)
        if (Path(target) == data / "runtime/realms/realm-1"
                and ".restore-" in Path(source).name):
            _kill("legacy-realm-replaced", kill_at)
        return result

    def copy2_and_kill(source, target, *args, **kwargs):
        result = copy2(source, target, *args, **kwargs)
        if (Path(source).name == "catalog.json" and Path(source).parent.name == "runtime"
                and Path(target).name.startswith(".restore-")):
            _kill("legacy-catalog-copy-staged", kill_at)
        return result

    def durable_replace_and_kill(source, target, *args, **kwargs):
        if (Path(target) == data / "runtime/catalog.json"
                and kill_at == "catalog-reconstitution-staged"):
            _kill(kill_at, kill_at)
        result = durable_replace(source, target, *args, **kwargs)
        if (Path(target) == data / "runtime/catalog.json"
                and kill_at == "catalog-reconstitution-published"):
            _kill(kill_at, kill_at)
        return result

    Installer._save_update = save_and_kill
    core.switch = switch_and_kill
    backend_module.os.replace = replace_and_kill
    backend_module.shutil.rmtree = rmtree_and_kill
    backend_module.shutil.copy2 = copy2_and_kill
    platform.replace_path = replace_path_and_kill
    platform.durable_replace = durable_replace_and_kill
    Path.unlink = unlink_and_kill
    backend = _Backend(base, kill_at)
    # Service manager effects are the one OS boundary patched in this proof;
    # ordinary update staging/registration/candidate start are deterministic
    # fixture boundaries. Recovery C1, restore, and the old Runtime launch/status
    # stay real; the old launch/status clears transaction context to avoid adding
    # an operation environment or tag to this amendment proof.
    backend.service.systemd = lambda: False
    backend.service.disable = lambda: None
    backend.service.stop = lambda: None
    backend.real_c1_recovery = (base / "program/backups/legacy-retained").is_dir()
    backend.real_runtime_proof = (base / "program/legacy-runtime-proof").is_file()
    backend.restart_required = lambda: False
    if mode == "cli-update":
        from astrid.installer import cli
        from runtime_protocol import platform

        platform.log_dir = lambda: base / "cli-logs"
        cli.Backend = lambda *_args, **_kwargs: backend
        result = cli.main([
            "--root", str(root), "--data-root", str(data), "--update",
            "--manifest", str(base / "unused-manifest.json"),
            "--uv", str(base / "unused-uv"),
        ])
        if result:
            raise RuntimeError(f"public installer Update exited with status {result}")
    else:
        transaction = read_json(root / "update-transaction.json")
        old_identity = transaction.get("old", {}).get("identity") if isinstance(transaction, dict) else None
        old_release = old_identity.get("release") if isinstance(old_identity, dict) else None
        manifest = ({"release": old_release if isinstance(old_release, int) else current(root).release}
                    if mode == "install" else {})
        Installer(root, data, backend, say=print).run(mode, manifest)


if __name__ == "__main__":
    mode, base, kill_at = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    if mode == "runtime":
        _runtime(base, kill_at)
    else:
        _installer(base, kill_at, mode)
