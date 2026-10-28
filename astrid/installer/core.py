"""Install transactions. Runtime owns workspace identity, migration and backups."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from .launcher import install as install_launcher
from .launcher import install_runtime
from .support import HELP_MESSAGE


class VerificationError(RuntimeError):
    """The installed Runtime's browser door did not pass its readiness check."""

    def __init__(self, message: str, *, safe_message: str | None = None) -> None:
        super().__init__(message)
        self.safe_message = safe_message


class RuntimeOperationError(RuntimeError):
    """A Runtime command failed, retaining its machine-readable report."""

    def __init__(self, message: str, *, details=None) -> None:
        super().__init__(message)
        self.details = details


def is_lifecycle_refusal(exc: BaseException) -> bool:
    """Recognize Runtime's explicit pre-destruction lifecycle report."""
    return (isinstance(exc, RuntimeOperationError)
            and isinstance(exc.details, dict) and exc.details.get("safe") is False)


class BackupSpaceError(RuntimeError):
    """The update cannot fit its workspace backup on the program disk."""


def linked(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        # Path.is_junction is unavailable on the pinned Python 3.11. This
        # also rejects other Windows reparse points before recursive copies.
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT"))
    except FileNotFoundError:
        return False


def absolute(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"An absolute path is required: {path}")
    path = Path(os.path.abspath(path))
    for entry in (path, *path.parents):
        if linked(entry):
            raise ValueError(f"Managed paths cannot contain links: {entry}")
    return path


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object in {path}")
    return value


def write_json(path: Path, value: dict) -> None:
    from runtime_protocol.platform import durable_replace

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    durable_replace(temporary, path)


TERMINAL_STEPS = frozenset({"complete", "rolled_back"})


def pending_transaction(root: Path) -> dict | None:
    """Return the update journal under root only while its step is not terminal."""
    journal = read_json(root / "update-transaction.json")
    if not journal or journal.get("step") in TERMINAL_STEPS:
        return None
    return journal


def owned_by_local_update(journal: dict) -> bool:
    context = journal.get("transaction_context")
    return isinstance(context, dict) and context.get("owner") == "local-update"


def separate_roots(root: Path, data: Path) -> bool:
    """Return whether program files and the workspace are disjoint directories."""
    return not (data == root or data in root.parents or root in data.parents)


def version_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
        raise ValueError("Invalid installation version identifier.")
    return value


@dataclass(frozen=True)
class Version:
    root: Path
    python: Path
    profile: Path
    env: dict[str, str]
    release: int = 0
    managed_by: str | None = None

    def __post_init__(self) -> None:
        if type(self.release) is not int or self.release < 0:
            raise ValueError("The install manifest release must be a nonnegative integer.")
        if self.managed_by not in {None, "pinokio"}:
            raise ValueError("Invalid installation manager.")
        env = dict(self.env)
        object.__setattr__(self, "env", env)
        env["ASTRID_INSTALL_RELEASE"] = str(self.release)
        env["ASTRID_INSTALL_MANAGED_BY"] = self.managed_by or ""

    def document(self) -> dict:
        return {"root": str(self.root), "python": str(self.python),
                "profile": str(self.profile), "env": dict(self.env),
                "identity": {"release": self.release, "managed_by": self.managed_by}}

    @classmethod
    def load(cls, value: dict) -> "Version":
        python = Path(value["python"])
        absolute(python.parent)  # POSIX venv interpreters themselves are links.
        root = absolute(value["root"])
        # Legacy reconstruction is read-only. Persist only after recovery and
        # successful stop admission (or a healthy repair requiring no stop).
        identity = value["identity"] if "identity" in value else install_identity(root)
        if not isinstance(identity, dict):
            raise ValueError("Invalid installation identity in version record.")
        release, managed_by = identity.get("release"), identity.get("managed_by")
        if type(release) is not int or release < 0 or managed_by not in {None, "pinokio"}:
            raise ValueError("Invalid installation identity in version record.")
        return cls(root, python, absolute(value["profile"]), value["env"], release, managed_by)


def install_release(manifest: dict) -> int:
    """Return the validated decimal release from a staged version manifest."""
    release = manifest.get("release", 0)
    if type(release) is not int or release < 0:
        raise ValueError("The install manifest release must be a nonnegative integer.")
    return release


def install_identity(version_root: Path) -> dict:
    """Derive a managed install's identity from its immutable manifest and marker."""
    managed_by = read_json(version_root.parent.parent / "install.json").get("managed_by")
    if managed_by not in {None, "pinokio"}:
        raise ValueError("Invalid installation manager in install record.")
    return {"release": install_release(read_json(version_root / "manifest.json")),
            "managed_by": managed_by}


def persist_install_identity(version: Version, program_root: Path, *,
                             selection: Path | None = None, fields: dict | None = None) -> None:
    """Stamp version metadata and merge the K2 identity at a caller's commit point."""
    record = read_json(selection) if selection is not None else {}
    if record and record.get("owner") != "haus.astrid.runtime":
        raise ValueError("The service record belongs to another installer.")
    if version.root.parent == program_root / "versions":
        path = version.root / "version.json"
        if read_json(path) != version.document():
            write_json(path, version.document())
    if selection is not None and (record or fields):
        updated = {**record, **(fields or {}), "identity": version.document()["identity"]}
        updated.pop("uninstalled", None)
        if updated != record:
            write_json(selection, updated)


def inspect_schema(backend, version: Version, realm: dict, *, require_current: bool = True) -> dict:
    """Read the selected Runtime's canonical workspace schema without mutation."""
    script = (
        "import json,sys; from pathlib import Path; "
        "from runtime_protocol.upgrade import inspect_canonical_schema, USER_APP_STATE_TARGET_SCHEMA_VERSION; "
        "value=inspect_canonical_schema(Path(sys.argv[1]), expected_realm_id=sys.argv[2]); "
        "print(json.dumps({'identity':value,'target':USER_APP_STATE_TARGET_SCHEMA_VERSION}))"
    )
    value = json.loads(backend.run(
        [version.python, "-X", "utf8", "-I", "-c", script, realm["realm_root"], realm["realm_id"]],
        extra_env=version.env, timeout=120,
    ))
    if require_current and value["identity"]["version"] != value["target"]:
        raise ValueError("This release needs a workspace migration. Choose Pinokio's Update button.")
    return value


def update_required(mode: str, manifest: dict, old: Version, *, managed_by: str | None = None) -> bool:
    """C2b numeric dispatch, shared by service and foreground adapters."""
    release = install_release(manifest)
    if release < old.release and mode != "update":
        if managed_by == "pinokio":
            raise ValueError(
                "This Astrid is newer than astrid.haus offers. Choose Pinokio's Update button when a newer release is available."
            )
        raise ValueError(
            "This Astrid is newer than astrid.haus offers. Try again when astrid.haus has a newer release."
        )
    return mode == "update" or release > old.release


def current(root: Path) -> Version | None:
    pointer = root / "current"
    if not pointer.exists():
        return None
    name = version_id(pointer.read_text(encoding="utf-8").strip())
    record = absolute(root / "versions" / name / "version.json")
    # Removal can delete version metadata before a locked pointer is released.
    # The service's retained selection can still finish uninstalling this root.
    if not record.exists() and not linked(record):
        return None
    version = Version.load(read_json(record))
    if version.root != root / "versions" / name:
        raise ValueError("The current version points outside the managed directory.")
    return version


def switch(root: Path, version: Version) -> None:
    from runtime_protocol.platform import durable_replace

    name = version_id(version.root.name)
    if version.root != root / "versions" / name:
        raise ValueError("The current pointer must name a managed version.")
    pointer = root / "current.tmp"
    with pointer.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(name + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    durable_replace(pointer, root / "current")


@contextmanager
def update_context(backend, transaction: dict, *, recovering: bool = False):
    """Expose the existing transaction solely for restore selection validation."""
    previous = getattr(backend, "_update_transaction", None)
    backend._update_transaction = (transaction, recovering)
    try:
        yield
    finally:
        backend._update_transaction = previous


def rollback_transaction(root: Path, data: Path, backend, transaction: dict, *,
                        stop, switch_old, activate_old, save,
                        restore_workspace: bool = True) -> None:
    """Apply the shared stop, optional restore, selection and ready rollback."""
    old = Version.load(transaction["old"])
    candidate = Version.load(transaction["candidate"]) if transaction.get("candidate") else None
    realm = transaction["realm"]
    backup = transaction.get("backup")
    kind = transaction.get("transaction_kind", "migration")
    with update_context(backend, transaction, recovering=True):
        # Before migration the old version owns manager cleanup. A later
        # migration can run the candidate before publishing its pointer.
        if kind == "app" or transaction.get("step") in {"recovering", "rollback_selected"}:
            stop_version = current(root) or old
        elif transaction.get("step") in {"stopping", "stopped"}:
            stop_version = old
        else:
            stop_version = candidate or old
        stop(stop_version, data)
        save("recovering")
        if backup is not None and restore_workspace and kind == "migration":
            backend.restore(old, data, realm, backup)
        switch_old(old, candidate)
        save("rollback_selected")
        activate_old(old, data, realm)
        save("rolled_back")


def recover_transaction(root: Path, data: Path, backend, *, stop, switch_old,
                       activate_old, restore_workspace: bool = True,
                       say=print, save=None) -> None:
    """Replay one interrupted update using its retained old version.

    A durable ``staged`` record is still pre-stop: recovery closes it without
    touching the currently running owner. ``stopping`` is the stop intent and
    must be fenced again because the process may have died between stop and
    publishing ``stopped``.
    """
    transaction = read_json(absolute(root / "update-transaction.json"))
    if not transaction or transaction.get("step") in TERMINAL_STEPS:
        return
    if (transaction.get("format") != "astrid-install-update/v1"
            or not isinstance(transaction.get("operation_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", transaction.get("operation_id", ""))
            or absolute(transaction["data_root"]) != absolute(data)):
        raise ValueError("The interrupted update identity does not match this installation.")
    if transaction.get("transaction_kind") not in {None, "migration", "app"}:
        raise ValueError("The interrupted update transaction kind is invalid.")
    if save is None:
        installer = Installer(root, data, backend)

        def save(step, **details):
            installer._save_update(transaction, step, **details)
    persist = save

    def save(step, **details):
        transaction.update(step=step, **details)
        persist(step, **details)

    # New journals publish staged before any stop or workspace backup. The old
    # v1 migration writer published staged only after stop and backup, so it
    # still needs the rollback path below.
    if (transaction.get("step") == "staged"
            and transaction.get("transaction_kind") in {"migration", "app"}
            and transaction.get("backup") is None):
        save("complete", outcome="recovered_before_stop")
        return
    realm = transaction["realm"]
    absolute(realm["realm_root"])
    backup = transaction.get("backup")
    if backup and "root" in backup:
        retained = absolute(backup["root"])
        if retained.parent != absolute(root / "backups"):
            raise ValueError("The interrupted update backup is outside this installation.")
    install_runtime(root)
    publish = getattr(backend, "publish_launch_admission", None)
    if publish is not None:
        publish(current(root) or Version.load(transaction["old"]), data)
    say("Recovering the interrupted Astrid update.")
    rollback_transaction(
        root, data, backend, transaction, stop=stop, switch_old=switch_old,
        activate_old=activate_old, restore_workspace=restore_workspace,
        save=save,
    )
    install_launcher(root)


class Installer:
    def __init__(self, root: Path, data: Path, backend, *, say=print, managed_by: str | None = None):
        self.root, self.data, self.backend, self.say = root, data, backend, say
        self.managed_by = managed_by

    def _activate(self, version: Version, realm_id: str) -> None:
        self.backend.register(version, self.data)
        if self.managed_by != "pinokio":
            self.backend.start(version, self.data)
            self.backend.verify(version, self.data, realm_id)
        elif self.backend.workspace(version, self.data)["realm_id"] != realm_id:
            raise VerificationError("The installed workspace identity changed.")

    def _publish_stop_admission(self, version: Version | None) -> None:
        publish = getattr(self.backend, "publish_launch_admission", None)
        if publish is not None and version is not None:
            install_runtime(self.root)
            publish(version, self.data)

    def _ready(self) -> None:
        if self.managed_by == "pinokio":
            self.say("Astrid is installed. Choose Pinokio's Start button.")
        else:
            self.say("Open https://astrid.haus")
            self.backend.open()

    def _save_update(self, transaction: dict, step: str, **details) -> None:
        transaction.update(step=step, **details)
        write_json(absolute(self.root / "update-transaction.json"), transaction)
        binding = transaction.get("update_job")
        if step == "staged" and isinstance(binding, dict):
            marker_path = absolute(self.root / "install.json")
            marker = read_json(marker_path)
            job = marker.get("update_job")
            if (not isinstance(job, dict) or job.get("operation_id") != binding.get("operation_id")
                    or job.get("state") != "active"):
                raise ValueError("The accepted update job no longer matches the C2 transaction.")
            job["transaction_id"] = transaction["operation_id"]
            marker["update_job"] = job
            write_json(marker_path, marker)

    def _recover_update(self) -> None:
        """Replay rollback before inspecting mutable support or choosing an operation."""
        pending = read_json(absolute(self.root / "update-transaction.json"))
        if pending and pending.get("step") not in TERMINAL_STEPS and owned_by_local_update(pending):
            raise ValueError("Use Astrid's local launcher to recover the interrupted app update.")
        if pending and pending.get("step") not in TERMINAL_STEPS \
                and pending.get("transaction_kind") == "app":
            raise ValueError("Choose Pinokio's Update button to recover the interrupted update.")

        def switch_old(old: Version, _candidate: Version | None) -> None:
            if current(self.root) in (None, old):
                return  # the pointer never moved; nothing to re-publish
            if old.root.parent == self.root / "versions":
                switch(self.root, old)
            else:
                (self.root / "current").unlink(missing_ok=True)

        recover_transaction(
            self.root, self.data, self.backend,
            stop=self.backend.stop, switch_old=switch_old,
            activate_old=lambda old, _data, realm: self._activate(old, realm["realm_id"]),
            say=self.say,
            save=lambda step, **details: self._save_update(pending, step, **details),
        )

    def _run_update_transaction(self, old: Version, realm: dict, *, prepare, stop,
                                 switch_candidate, switch_old, activate, activate_old,
                                 backup=None, migrate=None, restore_workspace: bool = True,
                                 kind: str = "migration",
                                 transaction_context: dict | None = None) -> Version:
        """Run the one durable stop/switch/ready/rollback policy for updates.

        ``prepare`` stages and validates while the old owner is still running.
        The caller owns app-specific lock handoffs inside the switch callbacks;
        all other transaction ordering and recovery state stays here.
        """
        with ExitStack() as authority:
            candidate = prepare()
            transaction = {
                "format": "astrid-install-update/v1", "operation_id": uuid.uuid4().hex,
                "transaction_kind": kind, "data_root": str(self.data),
                "old": old.document(), "realm": realm, "backup": None,
                "candidate": candidate.document(),
            }
            log = getattr(self.backend, "log", None)
            if isinstance(log, (str, Path)):
                transaction["log_path"] = str(absolute(Path(log)))
            update_job_operation_id = getattr(self.backend, "_update_job_operation_id", None)
            if update_job_operation_id:
                transaction["update_job"] = {"operation_id": update_job_operation_id}
            if transaction_context is not None:
                # Adapter-owned recovery context lives in the shared journal,
                # and is durable before any owner stop or checkout movement.
                transaction["transaction_context"] = transaction_context
            self._save_update(transaction, "staged")
            authority.enter_context(update_context(self.backend, transaction))
            install_runtime(self.root)
            publish = getattr(self.backend, "publish_launch_admission", None)
            if publish is not None:
                publish(old, self.data)
            # Record intent before the fenced stop: recovery can distinguish a
            # pure staged image from a stop that may have completed on crash.
            self._save_update(transaction, "stopping")

            def rollback_after(failure):
                try:
                    rollback_transaction(
                        self.root, self.data, self.backend, transaction,
                        stop=stop, switch_old=switch_old, activate_old=activate_old,
                        restore_workspace=restore_workspace,
                        save=lambda step, **details: self._save_update(
                            transaction, step, **details),
                    )
                except (Exception, KeyboardInterrupt) as recovery:
                    if is_lifecycle_refusal(recovery):
                        raise
                    combined = RuntimeError(
                        f"Update failed ({failure}); recovery also failed ({recovery}).")
                    combined.step = getattr(failure, "step", None) or "update"
                    raise combined from failure

            try:
                stop(old, self.data)
            except (Exception, KeyboardInterrupt) as failure:
                if is_lifecycle_refusal(failure):
                    try:
                        self._save_update(transaction, "complete", outcome="stop_refused")
                    except (Exception, KeyboardInterrupt) as journal_failure:
                        failure.add_note(
                            f"Could not durably cancel the staged update after the stop refusal: {journal_failure!r}")
                else:
                    rollback_after(failure)
                raise

            backup_image = None
            try:
                self._save_update(transaction, "stopped")
                if backup is not None:
                    backup_image = backup(old, self.data, realm)
                    transaction["backup"] = backup_image
                    self._save_update(transaction, "backed_up", backup=backup_image)
                if migrate is not None:
                    migrate(candidate, self.data, realm)
                    self._save_update(transaction, "migrated")
                switch_candidate(candidate)
                self._save_update(transaction, "switched")
                activate(candidate, self.data, realm)
                install_launcher(self.root)
                self._save_update(transaction, "verified")
                self._last_update_backup = backup_image
                if kind == "migration":
                    self.backend.finish(old, candidate, backup_image)
                self._save_update(transaction, "complete")
                return candidate
            except (Exception, KeyboardInterrupt) as failure:
                rollback_after(failure)
                raise

    def run(self, mode: str, manifest: dict, *, delete_data: bool = False,
            bundle: Path | None = None) -> None:
        if (self.managed_by == "pinokio" and mode in {"install", "update"}
                and current(self.root) is not None):
            from .app_update import update_pinokio

            return update_pinokio(self.root, self.backend, manifest, say=self.say,
                                  force=mode == "update")
        # App updates use the same durable journal but have a separate entry
        # point. Serialize operations that recover or write that journal. Doctor
        # and uninstall do not run journal recovery; a first install has no
        # existing root or journal to serialize.
        if mode in {"doctor", "uninstall"} or not self.root.is_dir():
            return self._run_locked(mode, manifest, delete_data=delete_data, bundle=bundle)
        from .app_update import locked

        with locked(self.root / ".app-update.lock"):
            return self._run_locked(mode, manifest, delete_data=delete_data, bundle=bundle)

    def _run_locked(self, mode: str, manifest: dict, *, delete_data: bool = False,
                    bundle: Path | None = None) -> None:
        if mode not in {"doctor", "uninstall"}:
            self._recover_update()
        old = current(self.root)
        if old is not None and not old.python.is_file():
            old = None
        old = old or self.backend.existing(self.data)
        # The public install path is also the normal update check. Compare
        # only after journal recovery and after resolving the active owner;
        # explicit ``update`` remains the forced restage operation.
        if old is not None and mode not in {"doctor", "uninstall"}:
            if update_required(mode, manifest, old, managed_by=self.managed_by):
                mode = "update"
        if mode == "doctor":
            if old is None:
                raise ValueError("No installed Astrid version is available for doctor.")
            destination = self.backend.doctor(old, self.data, bundle)
            self.say(f"Astrid doctor bundle: {destination}")
            self.say(HELP_MESSAGE)
            return
        if mode == "uninstall":
            self.backend.preflight_remove(self.root, self.data, delete_data=delete_data)
            self._publish_stop_admission(old)
            self.backend.stop(old, self.data)
            self.backend.unregister()
            residue = self.backend.remove(self.root, self.data, delete_data=delete_data)
            self.say("Astrid was uninstalled." if not residue else "Astrid was unregistered; some files remain.")
            for path in residue:
                self.say(f"Remaining: {path}")
            if not delete_data:
                self.say(f"Workspace kept at {self.data}.")
            return

        # Repair never provisions tools, installs code, migrates or rewrites data.
        if old and mode != "update":
            realm = self.backend.workspace(old, self.data)
            if self.managed_by == "pinokio":
                self.backend.register(old, self.data)
                install_launcher(self.root)
                try:
                    self.backend.sync_skills(old)
                except Exception:  # noqa: BLE001 - skill setup is best effort
                    self.say("Couldn't add Astrid's skill to your agents; see the log.")
                self._ready()
                return
            try:
                # The browser health endpoint refuses a changed Runtime identity
                # with runtime_identity_mismatch, which requires this restart.
                self.backend.verify(old, self.data, realm["realm_id"], timeout=3)
            except VerificationError:
                pass
            else:
                if not self.backend.restart_required():
                    # The only repair path without registration still stamps
                    # legacy records, after recovery and healthy verification.
                    persist = getattr(self.backend, "persist_identity", None)
                    if persist is not None:
                        persist(old)
                    install_launcher(self.root)
                    self.say("Open https://astrid.haus")
                    self.backend.open()
                    return
            self.say("Restarting Astrid with the installed version.")
            self._publish_stop_admission(old)
            self.backend.stop(old, self.data)
            self.backend.register(old, self.data)
            install_launcher(self.root)
            self.backend.start(old, self.data)
            self.backend.verify(old, self.data, realm["realm_id"])
            self.say("Open https://astrid.haus")
            self.backend.open()
            return

        has_workspace = self.backend.has_workspace(self.data)
        reinstall = old is None and has_workspace and self.backend.reinstallable(self.data)
        realm = self.backend.workspace(old, self.data) if old else None
        if not old and has_workspace and not reinstall:
            raise ValueError("An existing workspace has no usable source profile; repair its profile before installing.")
        if old:
            self.say("Installing Astrid.")

            def prepare() -> Version:
                candidate = self.backend.stage(manifest, self.root, self.data)
                self.backend.validate_candidate(candidate, self.data, realm)
                return candidate

            def migrate(candidate: Version, data: Path, _realm: dict) -> None:
                self.say("Updating the workspace with Astrid Runtime.")
                self.backend.upgrade(candidate, data)
                # upgrade owns its temporary daemon; return ownership to the
                # selected login service before switching versions.
                self.backend.stop(candidate, data)

            def select(candidate: Version) -> None:
                switch(self.root, candidate)

            def activate(candidate: Version, data: Path, selected_realm: dict) -> None:
                self.say("Finishing up." if self.managed_by == "pinokio" else "Starting Astrid at sign-in.")
                self.backend.register(candidate, data)
                if self.managed_by != "pinokio":
                    self.backend.start(candidate, data)
                    self.backend.verify(candidate, data, selected_realm["realm_id"])
                elif self.backend.workspace(candidate, data)["realm_id"] != selected_realm["realm_id"]:
                    raise VerificationError("The installed workspace identity changed.")
                try:
                    self.backend.sync_skills(candidate)
                except Exception:  # noqa: BLE001 - skill setup must never fail installation
                    self.say("Couldn't add Astrid's skill to your agents; see the log.")

            def select_old(previous: Version, _candidate: Version | None) -> None:
                if current(self.root) in (None, previous):
                    return  # the pointer never moved; nothing to re-publish
                if previous.root.parent == self.root / "versions":
                    switch(self.root, previous)
                else:
                    (self.root / "current").unlink(missing_ok=True)

            candidate = self._run_update_transaction(
                old, realm, prepare=prepare, stop=self.backend.stop,
                switch_candidate=select, switch_old=select_old,
                activate=activate,
                activate_old=lambda previous, _data, selected_realm: self._activate(
                    previous, selected_realm["realm_id"]),
                backup=self.backend.backup, migrate=migrate, kind="migration",
            )
            backup_image = getattr(self, "_last_update_backup", None)
            if backup_image:
                self.say(f"A backup of your workspace is kept at {backup_image['root']}.")
            self._ready()
            return

        candidate = None
        backup = None
        try:
            self.say("Installing Astrid.")
            candidate = self.backend.stage(manifest, self.root, self.data)
            if reinstall:
                realm = self.backend.attach_existing(candidate, self.data)
            else:
                realm = self.backend.create(candidate, self.data)
            switch(self.root, candidate)
            self.say("Finishing up." if self.managed_by == "pinokio" else "Starting Astrid at sign-in.")
            self.backend.register(candidate, self.data)
            install_launcher(self.root)
            if self.managed_by != "pinokio":
                self.backend.start(candidate, self.data)
            if self.managed_by != "pinokio":
                self.backend.verify(candidate, self.data, realm["realm_id"])
            else:
                # Creation/adoption/migration is verified through Runtime's
                # workspace API; Pinokio starts and checks the browser door.
                if self.backend.workspace(candidate, self.data)["realm_id"] != realm["realm_id"]:
                    raise VerificationError("The installed workspace identity changed.")
            try:
                self.backend.sync_skills(candidate)
            except Exception:  # noqa: BLE001 - skill setup must never fail installation
                self.say("Couldn't add Astrid's skill to your agents; see the log.")
            self.backend.finish(old, candidate, backup)
        except (Exception, KeyboardInterrupt) as failure:
            if is_lifecycle_refusal(failure):
                raise
            if candidate is not None:
                try:
                    self.backend.stop(candidate, self.data)
                except (Exception, KeyboardInterrupt) as cleanup:
                    if is_lifecycle_refusal(cleanup):
                        raise cleanup
                    failure.add_note(f"Stopping the failed installation also failed: {cleanup!r}")
            raise
        self._ready()


def new_version_id(manifest: dict) -> str:
    # A retry gets a fresh directory; incomplete/locked versions are never reused.
    return version_id(f"{manifest['agent']['sha'][:8]}-{manifest['runtime']['sha'][:8]}-{uuid.uuid4().hex[:8]}")
