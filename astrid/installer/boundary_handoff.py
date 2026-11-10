"""Serialized, copy-only R0 -> R1 handoff and data-preserving switch-back.

The per-user installer lock is held by the public entry for this entire
operation. The authoritative journal lives outside both roots, including before
Runtime publishes a destination. An interrupted command is NEVER inferred to
have succeeded from a destination or receipt. Recovery restores R0 and retains
every published copy. A completed handoff is normalised once (``normalise``):
R1 becomes the selected data root and the journal is archived as provenance,
which ``--rollback`` can still use until Astrid is updated.
"""
from __future__ import annotations

from contextlib import nullcontext

import hashlib
import json
import os
from pathlib import Path
import secrets

from . import adoption_receipt, boundary_registration as registration, r0_stop
from .core import Version, absolute, current, pending_transaction, read_json, separate_roots, version_id
from .installation import HANDOFF_ARCHIVE
from .service import LABEL, bootstrap_root


UPGRADE_UNINSTALL_REFUSAL = (
    "Astrid was upgraded from an earlier version; automatic uninstall after an upgrade isn't supported yet. "
    "Both data folders were kept."
)
# Release 1 has no qualified Windows boundary registration. The journey harness
# and evidence checker match this exact line in the installer output.
WINDOWS_UPGRADE_REFUSAL = (
    "Upgrading from an earlier Windows version isn't supported yet. "
    "Your data was kept; reinstall to get the new version."
)


def windows_upgrade_refused(root, service) -> bool:
    """Whether ``handle`` refuses this installation with WINDOWS_UPGRADE_REFUSAL.

    Mirrors the order in ``handle``: a pending transaction or a non-R0 install
    is never refused, and an R0 install on Windows always is.
    """
    return (service.system == "win32" and not pending_transaction(root)
            and r0_stop.is_r0_installation(root, service))


def journal_path(service):
    return r0_stop._marker_path(service).with_name("astrid-r1-handoff.json")


def archive_path(service):
    """A finished handoff's journal after normalisation: provenance, not authority."""
    return journal_path(service).with_name(HANDOFF_ARCHIVE)


def finish_restore(service):
    """Finish a restore_archive cut between its link and its unlink (same inode at both names)."""
    journal, archive = absolute(journal_path(service)), absolute(archive_path(service))
    if os.path.lexists(journal) and os.path.lexists(archive):
        if not os.path.samestat(os.lstat(journal), os.lstat(archive)):
            raise ValueError("Both an active and an archived storage move exist; both data folders were kept.")
        archive.unlink()
        r0_stop._sync_directory(archive.parent)


def retained_roots(service):
    roots = []
    for path in (journal_path(service), archive_path(service)):
        value = read_json(absolute(path))
        roots += [absolute(value[k]) for k in ("program_root", "r0_data_root", "r1_data_root")] if value else []
    return roots


def _text(path):
    return registration._regular(path).read_text(encoding="utf-8") if os.path.lexists(path) else None


def _json(value):
    return json.dumps(value, sort_keys=True) + "\n"


# Saved setup choices, the command receipt and service settings may change after a
# completed handoff; they are policy, not handoff identity.
POLICY_FIELDS = (("command_shim", "command_path", "update_job"),
                 ("choices", "service_settings", "chat_settings"), ())
ROLLBACK_AFTER_UPDATE = (
    "Astrid was updated after the earlier storage move, so switching back to that copy is not "
    "offered automatically. Both data folders were kept."
)


def _normalise_record(record, value, *, selection):
    """The direct-install form of a handed-off record: R1 is the data root, R0 is provenance."""
    result = {k: v for k, v in record.items() if k not in ("r1_data_root", "r1_realm_root")}
    result["data_root"] = value["r1_data_root"]
    result["retained_r0_data_root"] = value["r0_data_root"]
    if selection:
        result["realm_root"] = str(Path(value["r1_data_root"]) / "runtime/realms" / value["realm_id"])
    else:
        # The selection carries the selected identity; a marker copy would go stale on update.
        result.pop("identity", None)
    return result


def _matches(path, expected, ignore):
    text = _text(path)
    if text in expected:
        return True
    if text is None or not ignore:
        return False
    def strip(content):
        return {k: v for k, v in json.loads(content).items() if k not in ignore}
    return strip(text) in [strip(e) for e in expected]


class BoundaryHandoff:
    def __init__(self, backend):
        self.backend = backend
        self.service = backend.service
        self.root = absolute(self.service.root)
        self.journal = absolute(journal_path(self.service))
        self.registration = registration.BoundaryRegistration(self.service)

    def _save(self, value, *, first=False):
        registration._publish(self.journal, _json(value), exclusive=first,
            alias=registration._publication_alias(self.journal, value) if first else None)

    def _load(self):
        finish_restore(self.service)
        value = read_json(absolute(self.journal))
        if (value.get("version") != 1 or value.get("program_root") != str(self.root)
                or value.get("system") != self.service.system
                or value.get("phase") not in {"stopping", "staging", "adopting", "adopted",
                    "registering", "switching", "starting", "complete", "rolling_back", "rolled_back"}):
            raise ValueError("R1 handoff journal does not bind this installer; both roots were kept.")
        data = absolute(value["r0_data_root"])
        destination = absolute(value["r1_data_root"])
        realm = absolute(value["r0_realm"])
        if (destination != Path(str(data) + "-r1") or not realm.is_relative_to(data)
                or value["old_pointer"].strip() != Version.load(value["old_version"]).root.name
                or Version.load(value["old_version"]).root.parent != self.root / "versions"
                or Version.load(value["old_version"]).release != 0):
            raise ValueError("Invalid retained R0 handoff identity.")
        version_id(value["realm_id"])
        self.service.log = absolute(value["r1_log"])
        for path in (self.root, self.journal, self.service.selection, self.service.path,
                     self.registration.journal, self.registration.r1_path, self.service.log):
            if not separate_roots(data, absolute(path)) or not separate_roots(destination, absolute(path)):
                raise ValueError("Handoff state overlaps a retained data root.")
        marker = json.loads(value["old_marker"])
        selection = json.loads(value["old_selection"])
        if (marker.get("owner") != LABEL or marker.get("data_root") != str(data)
                or selection.get("owner") != LABEL or selection.get("program_root") != str(self.root)
                or selection.get("data_root") != str(data)
                or marker.get("managed_by") is not None or selection.get("managed_by") is not None
                or marker.get("r1_data_root") or selection.get("r1_data_root")):
            raise ValueError("Invalid saved R0 selection.")
        if value.get("candidate"):
            candidate = Version.load(value["candidate"])
            if (candidate.release < 1 or candidate.managed_by is not None
                    or candidate.root.parent != self.root / "versions"):
                raise ValueError("Invalid R1 candidate identity.")
        registration._publication_alias(self.journal, value)
        registration._finish_publication(self.journal, value, _json(value))
        return value

    def _records(self, value):
        candidate = Version.load(value["candidate"])
        identity = candidate.document()["identity"]
        common = {"r1_data_root": value["r1_data_root"], "identity": identity,
                  "service_label": registration.R1_LINUX if self.service.system == "linux" else registration.R1_MAC}
        marker = {**json.loads(value["old_marker"]), **common}
        selection = {**json.loads(value["old_selection"]), **common,
                     "realm_id": value["realm_id"], "realm_root": value["r0_realm"],
                     "r1_realm_root": str(Path(value["r1_data_root"]) / "runtime/realms" / value["realm_id"]),
                     "version": candidate.document()}
        selection.pop("uninstalled", None)
        return _json(marker), _json(selection), candidate.root.name + "\n"

    def _normalised_records(self, value):
        marker, selection, pointer = self._records(value)
        return (_json(_normalise_record(json.loads(marker), value, selection=False)),
                _json(_normalise_record(json.loads(selection), value, selection=True)), pointer)

    def _restore_selection(self, value):
        paths = (self.root / "install.json", self.service.selection, self.root / "current")
        old = (value["old_marker"], value["old_selection"], value["old_pointer"])
        new = self._records(value) if value.get("candidate") else old
        normal = self._normalised_records(value) if value.get("candidate") else old
        # Validate all three before the first mutation. A partial replay may
        # contain either side (or the normalised R1 side) at each file, never a
        # third party's replacement.
        for path, before, after, normalised, ignore in zip(paths, old, new, normal, POLICY_FIELDS):
            if not _matches(path, (before, after, normalised), ignore):
                raise ValueError(f"Handoff selection changed; R0 was not restarted: {path}")
        for path, before in zip(paths, old):
            if _text(path) != before:
                registration._publish(path, before, exclusive=False)

    def recover(self, *, rollback=False):
        if rollback and not os.path.lexists(self.journal) and os.path.lexists(archive_path(self.service)):
            self.restore_archive()  # Rollback from a normalised handoff, before any update.
        value = self._load()
        if value["phase"] == "rolled_back":
            self._restore_selection(value)
            r0_stop._require_no_r1(self.service)
            return value
        if value["phase"] == "complete" and not rollback:
            return value
        value["phase"] = "rolling_back"
        self._save(value)  # Covers interruptions in rollback itself.
        if os.path.lexists(self.registration.journal):
            bound = read_json(absolute(self.registration.journal))
            for key in ("program_root", "r1_data_root", "realm_id", "candidate", "old_pointer"):
                if bound.get(key) != value.get(key):
                    raise ValueError("R1 registration differs from its handoff; recovery intent retained.")
            if bound.get("r0_data_root") != value["r0_data_root"] or bound.get("r0_realm") != value["r0_realm"]:
                raise ValueError("R1 registration differs from its retained roots.")
            self.registration.recover(restore_selection=lambda: self._restore_selection(value))
        else:
            # No registration was published, or B already finished switch-back.
            # Do not infer this from a receipt, pointer, or process discovery.
            r0_stop._require_no_r1(self.service)
            if os.path.lexists(value["r1_data_root"]):
                r0_stop._wait_for_owner_lock(Path(value["r1_data_root"]) / "runtime/realms" / value["realm_id"], 90, .2)
            lock = (r0_stop.owner_lock(Path(value["r1_data_root"]) / "runtime/realms" / value["realm_id"])
                    if os.path.lexists(value["r1_data_root"]) else nullcontext())
            with lock:
                self._restore_selection(value)
                if os.path.lexists(r0_stop._marker_path(self.service)):
                    r0_stop.recover_r0(self.service)
                else:
                    if r0_stop._unit_digest(self.service) != value["r0_unit_sha256"]:
                        raise ValueError("R0 service changed; recovery intent retained.")
                    r0_stop._validate_manager(self.service, self.registration.uid)
        value["phase"] = "rolled_back"
        value["retained_copy"] = os.path.lexists(value["r1_data_root"])
        self._save(value)
        return value

    def validate_active(self, value):
        if value["phase"] != "complete" or not value.get("receipt"):
            raise ValueError("The R1 handoff is not complete.")
        for path, expected, normalised, ignore in zip(
                (self.root / "install.json", self.service.selection, self.root / "current"),
                self._records(value), self._normalised_records(value), POLICY_FIELDS):
            if not _matches(path, (expected, normalised), ignore):
                raise ValueError("R1 handoff selection changed; both roots were kept.")
        candidate = Version.load(value["candidate"])
        if read_json(registration._regular(candidate.root / "version.json")) != candidate.document():
            raise ValueError("R1 Version changed; both roots were kept.")
        if read_json(registration._regular(Path(value["r1_data_root"]) / adoption_receipt.RECEIPT)) != value["receipt"]:
            raise ValueError("R1 adoption receipt changed; both roots were kept.")
        if hashlib.sha256(registration._regular(candidate.profile).read_bytes()).hexdigest() != value["receipt"]["source_manifest_sha256"]:
            raise ValueError("R1 source profile changed; both roots were kept.")
        bound = read_json(registration._regular(self.registration.journal))
        if any(bound.get(k) != value[k] for k in ("candidate", "program_root", "r1_data_root", "realm_id")):
            raise ValueError("R1 service registration changed.")
        expected = self.registration._check_files(bound)
        if any(not path.exists() for path in expected):
            raise ValueError("R1 service registration is missing.")
        return candidate, Path(value["r1_data_root"])

    def normalise(self, value):
        """Finish a completed handoff once: afterwards this is an ordinary direct install.

        The marker and selection name R1 as ``data_root`` and its realm as
        ``realm_root``; R0 stays as ``retained_r0_data_root`` (provenance only).
        The journal becomes an archive. Each step is idempotent, so an
        interrupted normalisation is finished by the next run.
        """
        archive = absolute(archive_path(self.service))
        if os.path.lexists(archive):
            raise ValueError("An archived storage move already exists; both data folders were kept.")
        candidate, _destination = self.validate_active(value)
        # The selected version's launcher must accept the normalised records
        # before they are written: the R1 service restarts through it.
        registration.stage_runtime_launcher(candidate, replace=True)
        if _text(candidate.root / "bin/runtime.py") != registration.launcher_script(candidate):
            # Never write records the running version's launcher would refuse.
            raise ValueError("The selected version's launcher could not be replaced; nothing was changed.")
        for path, selection in ((self.root / "install.json", False), (self.service.selection, True)):
            record = json.loads(_text(path))
            normalised = _normalise_record(record, value, selection=selection)
            if normalised != record:
                registration._publish(path, _json(normalised), exclusive=False)
        os.replace(self.journal, archive)
        r0_stop._sync_directory(archive.parent)
        return candidate

    def restore_archive(self):
        """Make the archived journal authoritative again, only for --rollback from the handoff state."""
        archive = absolute(archive_path(self.service))
        value = read_json(registration._regular(archive))
        candidate = value.get("candidate")
        if (value.get("phase") != "complete" or not isinstance(candidate, dict)
                or _text(self.root / "current") != Path(candidate.get("root", "")).name + "\n"):
            raise ValueError(ROLLBACK_AFTER_UPDATE)
        if os.path.lexists(self.journal):
            raise ValueError("R1 handoff journal does not bind this installer; both roots were kept.")
        os.link(archive, self.journal, follow_symlinks=False)
        archive.unlink()
        r0_stop._sync_directory(archive.parent)

    def run(self, data, manifest_loader, *, say=print):
        data = absolute(data)
        destination = absolute(str(data) + "-r1")
        realm_id, realm = r0_stop._selected_realm(data)
        try:
            safe_log = all(separate_roots(tree, absolute(self.service.log)) for tree in (data, destination, self.root))
        except ValueError:
            safe_log = False
        if not safe_log:
            self.service.log = self.journal.parent / "runtime-r1.log"
        old = current(self.root)
        if old is None or old.release != 0:
            raise ValueError("Copy-only handoff requires the selected R0 Version.")
        if os.path.lexists(destination):
            raise ValueError(f"The R1 destination already exists: {destination}; preserve it before retrying.")
        value = {"version": 1, "publication_id": secrets.token_hex(16), "phase": "stopping",
                 "system": self.service.system, "program_root": str(self.root),
                 "r0_data_root": str(data), "r1_data_root": str(destination),
                 "r0_realm": str(realm), "realm_id": realm_id, "r1_log": str(self.service.log),
                 "r0_unit_sha256": r0_stop._unit_digest(self.service),
                 "old_pointer": _text(self.root / "current"), "old_version": old.document(),
                 "old_marker": _text(self.root / "install.json"),
                 "old_selection": _text(self.service.selection)}
        # Validate roots and initial records before publishing intent or stopping.
        for path in (self.root, self.journal, self.service.selection, self.service.path, self.service.log):
            if not separate_roots(data, absolute(path)) or not separate_roots(destination, absolute(path)):
                raise ValueError("Handoff files must be outside both data roots.")
        if value["old_marker"] is None or value["old_selection"] is None:
            raise ValueError("The R0 installation and selection records are required.")
        self._save(value, first=True)
        try:
            value = self._load()
            say("Astrid will restart to update.")
            r0_stop.stop_r0_and_wait(self.service, realm, data_root=data)
            value["phase"] = "staging"
            self._save(value)
            # Both service declarations and Version environment target R1.
            candidate = self.backend.stage(manifest_loader(), self.root, destination, copy_from=data)
            value["candidate"] = candidate.document()
            value["profile_sha256"] = hashlib.sha256(registration._regular(candidate.profile).read_bytes()).hexdigest()
            value["before"] = adoption_receipt.observe(data)
            value["phase"] = "adopting"
            self._save(value)  # An interruption from here is uncertain publication.
            result = self.backend.runtime(candidate, destination, "workspace", "adopt",
                "--from", str(data), "--realm-id", realm_id, "--read-only-source", "--no-start", profile=True)
            after = adoption_receipt.observe(data)
            if result.get("source_manifest_sha256") != value["profile_sha256"]:
                raise ValueError("Candidate profile changed during adoption; both roots were kept.")
            value["receipt"] = adoption_receipt.validate(result, value["before"], after,
                data=data, realm=realm, realm_id=realm_id, destination=destination, profile=candidate.profile)
            value["r1_data_root"] = str(Path(value["receipt"]["destination"]).parents[2])
            value["phase"] = "adopted"
            self._save(value)  # First durable confirmation of command success.
            value["phase"] = "registering"
            self._save(value)
            self.registration.prepare(candidate, data, realm_id, realm)
            with r0_stop.owner_lock(realm):
                # Recheck independent source evidence immediately before selection.
                if adoption_receipt.observe(data) != value["before"]:
                    raise ValueError("R0 changed before activation; both roots were kept.")
                value["phase"] = "switching"
                self._save(value)
                self._restore_selection(value)  # Validate the unchanged old snapshots.
                for path, content in zip((self.root / "install.json", self.service.selection, self.root / "current"),
                                         self._records(value)):
                    registration._publish(path, content, exclusive=False)
                # Diagnostic mirror is never recovery authority. The external intent
                # exists before adoption, and alone distinguishes uncertain success.
                registration._publish(destination / "update-journal.r1.json", _json({
                    "handoff_journal": str(self.journal), "receipt": value["receipt"], "state": "selected"}))
                value["phase"] = "starting"
                self._save(value)
                self.registration.activate()
                self.backend.verify(candidate, destination, realm_id)
            value["phase"] = "complete"
            self._save(value)
        except (Exception, KeyboardInterrupt):
            self.recover(rollback=True)
            raise
        # The handoff is committed; normalising cannot undo it. An interrupted
        # normalisation is finished by the next run from the complete journal.
        self.normalise(value)


POLICY_REFUSAL = ("The R0 to R1 handoff is copy-only and cannot apply setup choice changes "
                  "({names}). Nothing was changed; rerun without those flags, or change them after the handoff.")


def normalise_completed(backend):
    """Normalise a completed handoff journal once; return whether one was normalised."""
    if not os.path.lexists(journal_path(backend.service)):
        return False
    transaction = BoundaryHandoff(backend)
    value = transaction._load()
    if value["phase"] != "complete":
        return False
    transaction.normalise(value)
    return True


def handle(root, data, backend, *, manifest_loader=None, rollback=False, say=print, policy_change=None):
    """Return True when the public entry is handled without any legacy path.

    True means this run performed the copy-only handoff or recovered/rolled
    back one; the caller then reports the observed outcome. False means the
    install is not at the R0/R1 boundary (including a normalised handed-off
    install) and the ordinary install or update path continues.

    The handoff never changes command, autostart or skill policy. A requested
    change (``policy_change``) is therefore refused before any handoff work
    instead of being silently dropped; unchanged saved policy is carried over.
    """
    path = journal_path(backend.service)
    finish_restore(backend.service)
    if rollback and not os.path.lexists(path) and os.path.lexists(archive_path(backend.service)):
        BoundaryHandoff(backend).restore_archive()
    if not rollback and not os.path.lexists(path) and os.path.lexists(archive_path(backend.service)):
        # A normalised handed-off install is a direct install. Never replay the
        # retained R1 registration or R0 stop intent: they are rollback evidence.
        return False
    if os.path.lexists(path):
        transaction = BoundaryHandoff(backend)
        value = transaction._load()
        if value["phase"] == "complete" and not rollback:
            # The caller resolved its data root from the un-normalised records;
            # continuing would use R0's. The install line normalises first.
            raise ValueError("The earlier storage move must be finished by the install line first; "
                             "nothing was changed. Run the install line again.")
        if policy_change:
            raise ValueError(POLICY_REFUSAL.format(names=", ".join(sorted(policy_change))))
        if value["phase"] != "rolled_back" or rollback:
            value = transaction.recover(rollback=True)
            say("Recovered the previous R0 service. Both roots were kept; changes made in R1 remain in R1.")
            return True
        # The copy on disk decides, never the saved retained_copy flag: once the owner has
        # preserved it elsewhere, this clears the journal, or Update stays refused for good.
        if os.path.lexists(value["r1_data_root"]):
            raise ValueError(f"The previous R1 copy is retained at {value['r1_data_root']}. It cannot be reused or replaced automatically; preserve it before retrying.")
        transaction._restore_selection(value)
        path.unlink()
        r0_stop._sync_directory(path.parent)
    if rollback:
        raise ValueError("No R1 handoff is available to roll back.")
    # Preserve A/B's pre-C recovery semantics for their already-published intents.
    if os.path.lexists(registration.journal_path(backend.service)) and backend.recover_r1_registration():
        raise r0_stop.R0StopError("Recovered the interrupted R1 registration. Both roots were kept; run the install line again.")
    if r0_stop.recover_r0(backend.service):
        raise r0_stop.R0StopError("Recovered the interrupted R0 stop. Both roots were kept; run the install line again.")
    r0_stop.refuse_unintegrated_r1_handoff(root, backend.service)
    r0_stop.refuse_legacy_r0_recovery(root)
    if pending_transaction(root):
        return False  # Only a previously-verified R1 journal can reach this path.
    if not r0_stop.is_r0_installation(root, backend.service):
        return False
    if backend.service.system == "win32":
        # Refuse before the handoff journal, any stop, registration or copy.
        raise ValueError(WINDOWS_UPGRADE_REFUSAL)
    if manifest_loader is None:
        raise ValueError("The copy-only upgrade requires a candidate manifest.")
    if policy_change:
        raise ValueError(POLICY_REFUSAL.format(names=", ".join(sorted(policy_change))))
    BoundaryHandoff(backend).run(data, manifest_loader, say=say)
    return True


def uninstall_complete(backend, data, *, delete_data=False, say=print):
    """Refuse uninstall after an R0 to R1 handoff, preserving both installs."""
    raise ValueError(UPGRADE_UNINSTALL_REFUSAL)


def refuse_handoff_uninstall(service, root):
    """Fail closed on handoff records or residual R1 evidence."""
    registration_journal = registration.journal_path(service)
    r1_service = (service.path.with_name(registration.R1_LINUX)
                  if service.system == "linux" else service.path.with_name(registration.R1_MAC + ".plist")
                  if service.system == "darwin" else Path(root) / "task-r1.xml")
    if any(os.path.lexists(path) for path in
           (journal_path(service), archive_path(service), registration_journal, r1_service)):
        raise ValueError(UPGRADE_UNINSTALL_REFUSAL)
    try:
        marker = read_json(Path(root) / "install.json")
        selection = read_json(service.selection)
    except (OSError, TypeError, ValueError):
        raise ValueError(UPGRADE_UNINSTALL_REFUSAL) from None
    if any(record.get(key) for record in (marker, selection)
           for key in ("r1_data_root", "retained_r0_data_root")):
        raise ValueError(UPGRADE_UNINSTALL_REFUSAL)
