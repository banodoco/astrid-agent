"""Explicit, data-preserving recovery for non-continuable update evidence.

This is intentionally a small installer seam, not a second update engine.  It
does not reinterpret an old job or journal: it inventories that evidence,
keeps byte copies, and creates a distinct installation generation only after a
compatible copy of the whole data root has passed the normal readiness seam.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

from .backend import Backend
from .core import Version, absolute, current, inspect_schema, read_json, separate_roots, write_json
from .service import LABEL, install_selection


PLAN_FORMAT = "astrid-update-maintenance-plan/v1"
RECEIPT_FORMAT = "astrid-update-maintenance/v1"


class MaintenanceRefusal(ValueError):
    """A safe stop which leaves the original installation evidence untouched."""

    def __init__(self, reason: str, message: str, *, plan: dict | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.plan = plan


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _plan_digest(plan: dict) -> str:
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    return _sha256(_canonical(unsigned))


def _raw(path: Path, name: str) -> tuple[dict, bytes]:
    try:
        value = path.read_bytes()
    except FileNotFoundError:
        value = b""
        present = False
    except OSError as error:
        raise MaintenanceRefusal("unreadable-evidence", f"Cannot read retained {name} evidence: {error}") from error
    else:
        present = True
    return ({"name": name, "path": str(path), "present": present, "sha256": _sha256(value),
             "bytes": len(value)}, value)


def _load_owned_marker(root: Path, data: Path, selection: Path) -> dict:
    try:
        marker = read_json(root / "install.json")
        selected = read_json(selection)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise MaintenanceRefusal("unknown-ownership", "Installer ownership cannot be read safely.") from error
    if (marker.get("owner") != LABEL or marker.get("managed_by") is not None
            or selected.get("owner") != LABEL or selected.get("managed_by") is not None
            or selected.get("program_root") != str(root) or selected.get("data_root") != str(data)
            or marker.get("data_root") != str(data)):
        raise MaintenanceRefusal("unknown-ownership", "The selected installation ownership is not proven.")
    generation = marker.get("installation_generation", 0)
    if type(generation) is not int or generation < 0:
        raise MaintenanceRefusal("unknown-ownership", "The installation generation is not valid.")
    return marker


def _validate_paths(root: Path, data: Path) -> None:
    if not root.is_dir() or not data.is_dir():
        raise MaintenanceRefusal("unsafe-path", "Both program root and data root must be existing directories.")
    if not separate_roots(root, data):
        raise MaintenanceRefusal("unsafe-path", "Program root and data root must remain separate.")
    if root.parent == data.parent:
        # Sibling roots are fine.  This branch simply keeps the explicit path
        # check above close to the plan's no-link invariant.
        return


def _tree_manifest(root: Path) -> dict:
    """Hash every regular data file without following links or special files."""
    entries: list[dict[str, object]] = []
    try:
        for folder, directories, files in os.walk(root, topdown=True, followlinks=False):
            parent = Path(folder)
            directories.sort()
            files.sort()
            for name in directories:
                path = parent / name
                if path.is_symlink() or not stat.S_ISDIR(path.lstat().st_mode):
                    raise MaintenanceRefusal("unsafe-path", f"Data root contains an unsafe directory: {path}")
            for name in files:
                path = parent / name
                mode = path.lstat().st_mode
                if path.is_symlink() or not stat.S_ISREG(mode):
                    raise MaintenanceRefusal("unsafe-path", f"Data root contains an unsafe file: {path}")
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                entries.append({"path": str(path.relative_to(root)), "sha256": digest.hexdigest(),
                                "bytes": path.stat().st_size})
    except MaintenanceRefusal:
        raise
    except OSError as error:
        raise MaintenanceRefusal("data-inventory-failed", "The data root cannot be inventoried safely.") from error
    return {"entries": entries, "sha256": _sha256(_canonical(entries))}


def _evidence(root: Path, selection: Path) -> tuple[list[dict], dict[str, bytes]]:
    paths = (
        ("install", root / "install.json"),
        ("journal", root / "update-transaction.json"),
        ("current", root / "current"),
        ("selection", selection),
    )
    rows: list[dict] = []
    bytes_by_name: dict[str, bytes] = {}
    for name, path in paths:
        row, raw = _raw(path, name)
        rows.append(row)
        bytes_by_name[name] = raw
    return rows, bytes_by_name


def _evidence_digest(rows: list[dict]) -> str:
    return _sha256(_canonical(rows))


def _selected_version(root: Path) -> Version:
    try:
        selected = current(root)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise MaintenanceRefusal("unsafe-path", "The retained selected version cannot be verified.") from error
    if selected is None:
        raise MaintenanceRefusal("missing-recovery-bundle", "No retained compatible installation is selected.")
    return selected


def plan_update_recovery(root: Path | str, data_root: Path | str, *, recovery_bundle: Path | str | None = None) -> dict:
    """Return a deterministic, read-only maintenance plan and its review hash."""
    try:
        root_path, data = absolute(root), absolute(data_root)
        selection = absolute(install_selection(Path.home(), sys.platform))
    except (OSError, ValueError) as error:
        raise MaintenanceRefusal("unsafe-path", "Maintenance roots must be absolute, link-free paths.") from error
    _validate_paths(root_path, data)
    marker = _load_owned_marker(root_path, data, selection)
    selected = _selected_version(root_path)
    evidence, _ = _evidence(root_path, selection)
    data_manifest = _tree_manifest(data)
    operation = marker.get("update_job")
    operation_id = operation.get("operation_id") if isinstance(operation, dict) else None
    bundle = None
    if recovery_bundle is not None:
        try:
            path = absolute(recovery_bundle)
        except (OSError, ValueError) as error:
            raise MaintenanceRefusal("unsafe-path", "Recovery bundle path is unsafe.") from error
        row, _ = _raw(path, "recovery-bundle")
        bundle = row
    if marker.get("recovery_bundle_required") and (bundle is None or not bundle["present"]):
        bundle_state = "missing-recovery-bundle"
    else:
        bundle_state = None
    plan = {
        "format": PLAN_FORMAT,
        "root": str(root_path),
        "data_root": str(data),
        "selection_path": str(selection),
        "source_installation_generation": marker.get("installation_generation", 0),
        "new_installation_generation": marker.get("installation_generation", 0) + 1,
        "source_operation_id": operation_id if isinstance(operation_id, str) else None,
        "selected_version": selected.document(),
        "source_evidence": evidence,
        "source_evidence_sha256": _evidence_digest(evidence),
        "source_data_manifest_sha256": data_manifest["sha256"],
        "recovery_bundle": bundle,
        "blocked_reason": bundle_state,
        # A6 is deliberately an external release qualification.  This field
        # documents the dependency; it is not a local approval token.
        "external_qualification": ["A6", "T22", "T23"],
        "steps": ["prove-ownership", "fence-native-writers", "verify-schema",
                  "preserve-evidence", "copy-and-verify-data", "verify-ready", "publish-generation"],
    }
    plan["plan_sha256"] = _plan_digest(plan)
    return plan


def _assert_same_plan(plan: dict, supplied_hash: str) -> None:
    if not isinstance(supplied_hash, str) or supplied_hash != plan.get("plan_sha256"):
        raise MaintenanceRefusal("plan-mismatch", "The reviewed maintenance plan changed; generate and review a new plan.",
                                 plan=plan)
    if _plan_digest(plan) != supplied_hash:
        raise MaintenanceRefusal("plan-mismatch", "The maintenance plan hash is invalid.", plan=plan)


def _schema(backend: Backend, version: Version, data: Path) -> tuple[dict, dict]:
    try:
        hook = getattr(backend, "maintenance_schema", None)
        if callable(hook):
            realm, schema = hook(version, data)
        else:
            realm = backend.workspace(version, data)
            schema = inspect_schema(backend, version, realm, require_current=False)
        if not isinstance(realm, dict) or not isinstance(realm.get("realm_id"), str):
            raise ValueError("workspace realm is not known")
        if not isinstance(schema, dict) or not schema:
            raise ValueError("workspace schema is not known")
        return realm, schema
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise MaintenanceRefusal("unknown-schema", "The data schema cannot be verified safely.") from error


def _quiesce(backend: Backend, version: Version, data: Path) -> None:
    try:
        hook = getattr(backend, "maintenance_quiesce", None)
        result = hook(version, data) if callable(hook) else backend.preflight_stop(data)
        if result is False:
            raise ValueError("native stop remained unknown")
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        raise MaintenanceRefusal("unknown-stop", "Native writer quiescence is not proven; stop or restart it, then rerun this plan.") from error


def _copy_tree(source: Path, destination: Path) -> None:
    def copy_file(src: str, dst: str) -> str:
        source_path = Path(src)
        if source_path.is_symlink() or not stat.S_ISREG(source_path.lstat().st_mode):
            raise MaintenanceRefusal("unsafe-path", f"Data copy refused unsafe input: {source_path}")
        shutil.copy2(source_path, dst)
        return dst

    try:
        shutil.copytree(source, destination, copy_function=copy_file, symlinks=False)
    except MaintenanceRefusal:
        raise
    except (OSError, shutil.Error) as error:
        raise MaintenanceRefusal("copy-interrupted", "The whole data-root copy did not complete; the original remains untouched.") from error


def _attempt(root: Path, repair_id: str) -> tuple[Path, int]:
    base = root / "maintenance" / repair_id
    number = 1
    while (base / f"attempt-{number}").exists():
        number += 1
    return base / f"attempt-{number}", number


def _new_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise MaintenanceRefusal("unsafe-path", f"Maintenance target already exists: {path}")
    with path.open("xb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _archive_raw(attempt: Path, evidence: dict[str, bytes], rows: list[dict]) -> None:
    archive = attempt / "raw-evidence"
    for name, value in evidence.items():
        _new_bytes(archive / name, value)
    _new_bytes(archive / "inventory.json", _canonical({"evidence": rows}) + b"\n")


def _ready(backend: Backend, version: Version, data: Path, realm: dict) -> tuple[str, dict]:
    try:
        hook = getattr(backend, "maintenance_ready", None)
        if callable(hook):
            ready = hook(version, data, realm)
        else:
            backend.start(version, data)
            backend.verify(version, data, realm["realm_id"])
            ready = read_json(data / "runtime" / "discovery.json")
        if not isinstance(ready, dict):
            raise ValueError("ready instance is not reported")
        instance_id = ready.get("runtime_instance_id") or ready.get("ready_instance_id")
        identity = ready.get("identity", version.document()["identity"])
        if not isinstance(instance_id, str) or not instance_id or not isinstance(identity, dict):
            raise ValueError("ready instance identity is not reported")
        return instance_id, identity
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        raise MaintenanceRefusal("verification-failed", "The compatible installation did not become ready; no generation was published.") from error


def _publish(root: Path, selection: Path, marker: dict, plan: dict, receipt_path: Path,
             working_data: Path, receipt: dict) -> None:
    # Recheck raw ownership immediately before the only writes to selected
    # state.  The archived source record remains byte-for-byte available.
    try:
        live = read_json(root / "install.json")
        selected = read_json(selection)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise MaintenanceRefusal("unknown-ownership", "Ownership changed before maintenance settlement.") from error
    if live != marker or selected.get("owner") != LABEL or selected.get("program_root") != str(root):
        raise MaintenanceRefusal("unknown-ownership", "Ownership changed before maintenance settlement.")
    settlement = {"repair_id": receipt["repair_id"], "receipt": str(receipt_path),
                  "plan_sha256": plan["plan_sha256"], "disposition": receipt["disposition"]}
    updated_marker = {**marker, "installation_generation": plan["new_installation_generation"],
                      "maintenance_settlement": settlement}
    updated_selection = {**selected, "data_root": str(working_data),
                         "installation_generation": plan["new_installation_generation"],
                         "maintenance_settlement": settlement}
    # The unresolved job/journal fields are intentionally neither rewritten nor
    # relabelled.  New state is an independent installation generation.
    write_json(root / "install.json", updated_marker)
    try:
        write_json(selection, updated_selection)
    except BaseException:
        # A durable receipt and old raw archive make this an explicit resumable
        # settlement, rather than a hidden clearing of the unresolved job.
        raise


def apply_update_recovery(root: Path | str, data_root: Path | str, plan_sha256: str, *,
                          recovery_bundle: Path | str | None = None, backend: Backend | None = None) -> dict:
    """Apply one reviewed maintenance plan without mutating unresolved evidence.

    The caller must keep the returned plan hash stable.  Every failure raises
    :class:`MaintenanceRefusal`; source data and source update evidence are not
    cleared, overwritten, or reclassified on any such path.
    """
    plan = plan_update_recovery(root, data_root, recovery_bundle=recovery_bundle)
    _assert_same_plan(plan, plan_sha256)
    if plan["blocked_reason"]:
        raise MaintenanceRefusal(plan["blocked_reason"], "A verified matching recovery bundle is required before apply.",
                                 plan=plan)
    root_path, data = Path(plan["root"]), Path(plan["data_root"])
    selection = Path(plan["selection_path"])
    selected = Version.load(plan["selected_version"])
    active_backend = backend or Backend(root_path, root_path / "logs" / "maintenance.log",
                                        root_path / "bootstrap" / "uv")

    # All gates happen before archive/copy/publish writes.  A caller can repair
    # a missing bundle, unsafe path, schema, or stop proof and rerun the plan.
    _quiesce(active_backend, selected, data)
    source_realm, source_schema = _schema(active_backend, selected, data)
    rows, raw_evidence = _evidence(root_path, selection)
    if _evidence_digest(rows) != plan["source_evidence_sha256"]:
        raise MaintenanceRefusal("plan-mismatch", "Retained update evidence changed; review a new plan.", plan=plan)
    source_manifest = _tree_manifest(data)
    if source_manifest["sha256"] != plan["source_data_manifest_sha256"]:
        raise MaintenanceRefusal("plan-mismatch", "Data changed since planning; review a new plan.", plan=plan)

    repair_id = plan["plan_sha256"][:32]
    attempt, number = _attempt(root_path, repair_id)
    preserved = data.with_name(f"{data.name}.astrid-preserved-{repair_id}-{number}")
    working = data.with_name(f"{data.name}.astrid-working-{repair_id}-{number}")
    if preserved.exists() or working.exists() or preserved.is_symlink() or working.is_symlink():
        raise MaintenanceRefusal("unsafe-path", "A maintenance data-copy target already exists; rerun the reviewed plan.", plan=plan)
    _archive_raw(attempt, raw_evidence, rows)
    _copy_tree(data, preserved)
    _copy_tree(data, working)
    preserved_manifest = _tree_manifest(preserved)
    working_manifest = _tree_manifest(working)
    after_copy = _tree_manifest(data)
    if (preserved_manifest["sha256"] != source_manifest["sha256"]
            or working_manifest["sha256"] != source_manifest["sha256"]
            or after_copy["sha256"] != source_manifest["sha256"]):
        raise MaintenanceRefusal("copy-verification-failed", "The preserved and working data copies could not be verified.",
                                 plan=plan)
    working_realm, working_schema = _schema(active_backend, selected, working)
    if working_schema != source_schema:
        raise MaintenanceRefusal("unknown-schema", "The working data copy has a different schema from the preserved data.",
                                 plan=plan)
    ready_instance_id, ready_identity = _ready(active_backend, selected, working, working_realm)

    receipt = {
        "format": RECEIPT_FORMAT,
        "repair_id": repair_id,
        "plan_sha256": plan["plan_sha256"],
        "source_installation_generation": plan["source_installation_generation"],
        "source_operation_id": plan["source_operation_id"],
        "source_evidence_sha256": plan["source_evidence_sha256"],
        "preserved_data_manifest_sha256": preserved_manifest["sha256"],
        "working_data_manifest_sha256": working_manifest["sha256"],
        "selected_version_sha256": _sha256(_canonical(selected.document())),
        "ready_instance_id": ready_instance_id,
        "ready_identity": ready_identity,
        "new_installation_generation": plan["new_installation_generation"],
        "disposition": "restored-working-installation",
        "old_operation_disposition": "quarantined-unresolved",
        "preserved_data_path": str(preserved),
        "working_data_path": str(working),
        "source_realm_id": source_realm["realm_id"],
    }
    receipt_path = attempt / "maintenance-receipt.json"
    _new_bytes(receipt_path, _canonical(receipt) + b"\n")
    marker = _load_owned_marker(root_path, data, selection)
    _publish(root_path, selection, marker, plan, receipt_path, working, receipt)
    return {"state": "restored-working-installation", "plan": plan, "receipt": receipt,
            "receipt_path": str(receipt_path)}
