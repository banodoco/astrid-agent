"""Install transactions. Runtime owns workspace identity, migration and backups."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from .launcher import install as install_launcher
from .support import HELP_MESSAGE


class VerificationError(RuntimeError):
    """The installed Runtime's browser door did not pass its readiness check."""


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

    def document(self) -> dict:
        return {"root": str(self.root), "python": str(self.python),
                "profile": str(self.profile), "env": self.env}

    @classmethod
    def load(cls, value: dict) -> "Version":
        python = Path(value["python"])
        absolute(python.parent)  # POSIX venv interpreters themselves are links.
        return cls(absolute(value["root"]), python,
                   absolute(value["profile"]), dict(value["env"]))


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


class Installer:
    def __init__(self, root: Path, data: Path, backend, *, say=print):
        self.root, self.data, self.backend, self.say = root, data, backend, say

    def _save_update(self, transaction: dict, step: str, **details) -> None:
        transaction.update(step=step, **details)
        write_json(absolute(self.root / "update-transaction.json"), transaction)

    def _recover_update(self) -> None:
        """Replay rollback from its retained image before inspecting mutable support."""
        transaction = read_json(absolute(self.root / "update-transaction.json"))
        if not transaction or transaction.get("step") in {"complete", "rolled_back"}:
            return
        if (transaction.get("format") != "astrid-install-update/v1"
                or not re.fullmatch(r"[0-9a-f]{32}", str(transaction.get("operation_id", "")))
                or absolute(transaction["data_root"]) != absolute(self.data)):
            raise ValueError("The interrupted update identity does not match this installation.")
        old = Version.load(transaction["old"])
        candidate = Version.load(transaction["candidate"]) if transaction.get("candidate") else None
        realm = transaction["realm"]
        absolute(realm["realm_root"])
        backup = transaction.get("backup")
        if backup and "root" in backup:
            retained = absolute(backup["root"])
            if retained.parent != absolute(self.root / "backups"):
                raise ValueError("The interrupted update backup is outside this installation.")
        self.say("Recovering the interrupted Astrid update.")
        self._save_update(transaction, "recovering")
        # These actions can themselves be interrupted. Keep the transaction and
        # image until the old owner has passed verification; restore is replayable.
        self.backend.stop(candidate or old, self.data)
        if backup is not None:
            self.backend.restore(old, self.data, realm, backup)
        if old.root.parent == self.root / "versions":
            switch(self.root, old)
        else:
            (self.root / "current").unlink(missing_ok=True)
        self.backend.register(old, self.data)
        self.backend.start(old, self.data)
        self.backend.verify(old, self.data, realm["realm_id"])
        self._save_update(transaction, "rolled_back")

    def run(self, mode: str, manifest: dict, *, delete_data: bool = False,
            bundle: Path | None = None) -> None:
        if mode not in {"doctor", "uninstall"}:
            self._recover_update()
        old = current(self.root)
        if old is not None and mode not in {"doctor", "uninstall"}:
            install_launcher(self.root)
        if old is not None and not old.python.is_file():
            old = None
        old = old or self.backend.existing(self.data)
        if mode == "doctor":
            if old is None:
                raise ValueError("No installed Astrid version is available for doctor.")
            destination = self.backend.doctor(old, self.data, bundle)
            self.say(f"Astrid doctor bundle: {destination}")
            self.say(HELP_MESSAGE)
            return
        if mode == "uninstall":
            self.backend.preflight_remove(self.root, self.data, delete_data=delete_data)
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
            try:
                # The browser health endpoint refuses a changed Runtime identity
                # with runtime_identity_mismatch, which requires this restart.
                self.backend.verify(old, self.data, realm["realm_id"], timeout=3)
            except VerificationError:
                pass
            else:
                if not self.backend.restart_required():
                    self.say("Open https://astrid.haus")
                    self.backend.open()
                    return
            self.say("Restarting Astrid with the installed version.")
            self.backend.stop(old, self.data)
            self.backend.register(old, self.data)
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
        backup = None
        candidate = None
        stopped = False
        switched = False
        transaction = None
        try:
            if old:
                transaction = {
                    "format": "astrid-install-update/v1", "operation_id": uuid.uuid4().hex,
                    "data_root": str(self.data), "old": old.document(), "realm": realm,
                    "backup": None, "candidate": None,
                }
                self._save_update(transaction, "prepared")
                self.say("Stopping Astrid before the update.")
                # A stop can fail after stopping the manager: recovery must run.
                stopped = True
                self.backend.stop(old, self.data)
                self._save_update(transaction, "stopped")
                self.say("Backing up your workspace.")
                backup = self.backend.backup(old, self.data, realm)
                self._save_update(transaction, "backed_up", backup=backup)
            self.say("Installing Astrid.")
            candidate = self.backend.stage(manifest, self.root, self.data)
            if old:
                assert transaction is not None
                self._save_update(transaction, "staged", candidate=candidate.document())
                self.say("Updating the workspace with Astrid Runtime.")
                self.backend.upgrade(candidate, self.data)
                self._save_update(transaction, "upgraded")
                # upgrade owns its temporary daemon; hand back to the login service.
                self.backend.stop(candidate, self.data)
            elif reinstall:
                realm = self.backend.attach_existing(candidate, self.data)
            else:
                realm = self.backend.create(candidate, self.data)
            switch(self.root, candidate)
            switched = True
            install_launcher(self.root)
            if transaction is not None:
                self._save_update(transaction, "switched")
            self.say("Starting Astrid at sign-in.")
            self.backend.register(candidate, self.data)
            if transaction is not None:
                self._save_update(transaction, "registered")
            self.backend.start(candidate, self.data)
            if transaction is not None:
                self._save_update(transaction, "started")
            self.backend.verify(candidate, self.data, realm["realm_id"])
            if transaction is not None:
                self._save_update(transaction, "verified")
            try:
                self.backend.sync_skills(candidate)
            except Exception:  # noqa: BLE001 - skill setup must never fail installation
                self.say("Couldn't add Astrid's skill to your agents; see the log.")
            self.backend.finish(old, candidate, backup)
            if transaction is not None:
                self._save_update(transaction, "complete")
        except (Exception, KeyboardInterrupt) as failure:
            if old and stopped:
                try:
                    self.backend.stop(candidate or old, self.data)
                    if backup is not None:
                        self.backend.restore(old, self.data, realm, backup)
                    pointer_changed = switched or current(self.root) not in (None, old)
                    if pointer_changed and old.root.parent == self.root / "versions":
                        switch(self.root, old)
                    elif pointer_changed:
                        (self.root / "current").unlink(missing_ok=True)
                    self.backend.register(old, self.data)
                    self.backend.start(old, self.data)
                    self.backend.verify(old, self.data, realm["realm_id"])
                    if transaction is not None:
                        self._save_update(transaction, "rolled_back")
                except (Exception, KeyboardInterrupt) as recovery:
                    raise RuntimeError(f"Update failed ({failure}); recovery also failed ({recovery}).") from failure
            elif candidate is not None:
                try:
                    self.backend.stop(candidate, self.data)
                except (Exception, KeyboardInterrupt) as cleanup:
                    failure.add_note(f"Stopping the failed installation also failed: {cleanup!r}")
            raise
        if backup:
            self.say(f"A backup of your workspace is kept at {backup['root']}.")
        self.say("Open https://astrid.haus")
        self.backend.open()


def new_version_id(manifest: dict) -> str:
    # A retry gets a fresh directory; incomplete/locked versions are never reused.
    return version_id(f"{manifest['agent']['sha'][:8]}-{manifest['runtime']['sha'][:8]}-{uuid.uuid4().hex[:8]}")
