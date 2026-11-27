"""Installed release defaults through the existing canonical group transaction.

This adapter runs in the real version's noneditable interpreter. Preparation
is private; application is called only after the installer's existing stop
fence. A write-ahead group receipt makes application and rollback replayable.
No acquisition, collection exchange, or build policy is implemented here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from importlib import metadata
from pathlib import Path
from typing import Any

from astrid.core.pack.identity import _validate_pin, pack_digest, read_pin
from astrid.core.pack.install import PackInstallError, _git_source
from astrid.core.pack.release_manifest import (
    _default_pins, install_release_manifest, installed_release_manifest,
    load_release_manifest, release_pins, verify_release_default,
)
from astrid.core.pack.transaction import group_read_lock, read_group, rollback_group
from astrid.installer.core import absolute

_RECEIPT = "default-packs.json"
_PREPARED = "default-packs-prepared.json"
_SCHEMA = "astrid-installed-defaults/v1"


def _read_record(path: Path, *, required: bool = False) -> dict:
    """Receipts cannot follow aliases or silently accept duplicate JSON keys."""
    from runtime_protocol import platform

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate receipt field.")
            result[key] = value
        return result

    try:
        absolute(path)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or not platform.is_current_user(info)
                or (os.name != "nt" and info.st_mode & 0o022)
                or (os.name == "nt" and not platform.is_owner_only(path))):
            raise ValueError("Untrusted receipt file.")
        value = json.loads(path.read_bytes(), object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise ValueError("Receipt must be an object.")
        return value
    except FileNotFoundError:
        if not required:
            return {}
        raise PackInstallError("invalid_state", "The installed default-delivery receipt is missing.") from None
    except (OSError, ValueError, TypeError) as exc:
        raise PackInstallError("invalid_state", "The installed default-delivery receipt is invalid.") from exc


def _write_record(path: Path, value: dict) -> None:
    from runtime_protocol import platform

    absolute(path)
    fd, temporary = platform.private_temporary(path.parent, ".default-packs-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write((json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        platform.durable_replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _packages() -> dict[str, str]:
    return {dist.metadata["Name"].lower().replace("_", "-").replace(".", "-"): dist.version
            for dist in metadata.distributions() if dist.metadata.get("Name")}


def _manifest_hash(folder: Path) -> str:
    return hashlib.sha256((folder / "manifest.json").read_bytes()).hexdigest()


def _collection() -> Path:
    # The same canonical writable collection as packs add/remove and the host.
    # ASTRID_PACKS_PATH never changes the identity of this collection.
    from astrid.core.pack.cli_manage import _selected_root
    return _selected_root(None)


def _version() -> tuple[Path, dict[str, Any]]:
    installed = installed_release_manifest(selected=False)
    if installed is None:
        raise PackInstallError("invalid_release_manifest", "Default delivery requires the version's installed interpreter.")
    return installed


def _rendering_proof(root: Path) -> list[dict[str, Any]]:
    """Validate the declared rendering union with the existing strict compiler.

    Developer preview programs are retained and hashed by pack-tree-v1, but
    are not passed off as project browser modules. The project-pack gate is
    unchanged and continues to reject unsupported preview code loading.
    """
    from astrid.core.element import project_packs as pp
    from astrid.pack_build import compile_element_module

    entry, resources = pp._resources(root, expected_pack_id=root.name)
    rendering = entry.definition.rendering
    if not rendering:
        return []
    names = {item["path"] for item in rendering.values()}
    names.update(resource["path"] for item in rendering.values() for resource in item.get("resources", ()))
    pp._check_imports(root, {name: resources[name] for name in sorted(names)})
    node, api = pp._renderer_compiler()
    contract = Path(__file__).resolve().parents[1] / "packs/rendering/shared/element_contracts.ts"
    before, contract_hash = pack_digest(root), hashlib.sha256(contract.read_bytes()).hexdigest()
    results = []
    with tempfile.TemporaryDirectory(prefix="astrid-default-compile-") as directory:
        for index, (definition, _descriptor) in enumerate(pp._definitions(root, entry)):
            component = definition.component.relative_to(root).as_posix()
            if component not in names:
                raise ValueError("Rendering entry is outside its declared resource union.")
            output = compile_element_module(node, api, root, component, pp._node_env(), rendering_contract=contract)
            if not output.strip():
                raise ValueError("Rendering compiler returned an empty module.")
            target = Path(directory) / f"{index}.mjs"
            target.write_bytes(output)
            checked = subprocess.run([str(node), "--check", str(target)], env=pp._node_env(),
                                     stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
            if checked.returncode:
                raise ValueError("Compiled rendering module failed Node syntax validation.")
            results.append({"id": definition.id, "component": component, "bytes": len(output),
                            "sha256": hashlib.sha256(output).hexdigest(), "syntax_exit": checked.returncode,
                            "contract_sha256": contract_hash})
    if pack_digest(root) != before or hashlib.sha256(contract.read_bytes()).hexdigest() != contract_hash:
        raise ValueError("Rendering validation changed its source or selected contract.")
    return results


def _install(manifest: dict, root: Path, *, on_prepared=None, boundary=None):
    from astrid.core.pack.cli_manage import _build_inputs, _check_staged_builds, _consent_details, TOOLCHAIN_ENV

    source, revision, packs, _digests = _default_pins(manifest)
    builds, toolchain = (_build_inputs(source, revision, packs, os.environ.get(TOOLCHAIN_ENV))
                         if packs else ({}, None))
    proofs: dict[str, Any] = {}

    def consent(changes):
        # The enclosing install/update consent selects this exact release.
        # It grants no pack capability or secret access. Bind that selection
        # to the staged identities before any builder or publication runs.
        _check_staged_builds(changes, builds)
        for pack_id, change in changes["packs"].items():
            pin = manifest["packs"][pack_id]
            if (change["after"]["commit"] != pin["commit"]
                    or change["after_digest"] != pin["digest"]
                    or change["after"]["origin"] != "default"):
                raise PackInstallError("pin_mismatch", "Staged defaults differ from the selected release.")
            staged = Path(change["staged_root"])
            proofs[pack_id] = {"permissions": _consent_details(staged, source=source, commit=revision),
                               "rendering": _rendering_proof(staged)}
        return True

    result = install_release_manifest(manifest=manifest, packs_root=root, python_packages=_packages(),
                                      builds=builds, toolchain=toolchain, consent=consent,
                                      on_prepared=on_prepared, boundary=boundary)
    return result, proofs


def prepare() -> dict:
    folder, manifest = _version()
    if "packs" not in manifest:
        return {"status": "legacy-no-default-list"}
    root = folder / "default-packs-stage" / "packs"
    result, proofs = _install(manifest, root)
    record = {"schema": _SCHEMA, "manifest_sha256": _manifest_hash(folder),
              "transaction_id": result.transaction_id, "revision": result.revision, "proofs": proofs}
    _write_record(folder / _PREPARED, record)
    return record


def _matches(manifest: dict, root: Path) -> bool:
    with group_read_lock(root):
        return _matches_locked(manifest, root)


def _matches_locked(manifest: dict, root: Path) -> bool:
    snapshot = read_group(root)
    wanted = manifest.get("packs", {})
    for pack_id, pin in wanted.items():
        if pack_id in snapshot.removals:
            continue
        path = snapshot.paths.get(pack_id)
        if path is None:
            return False
        try:
            verify_release_default(path, pin)
        except (OSError, ValueError, PackInstallError):
            return False
    for pack_id, path in snapshot.paths.items():
        if pack_id not in wanted and (path / "PIN").is_file() and read_pin(path / "PIN")["origin"] == "default":
            return False
    return True


def needs_repair() -> dict:
    folder, manifest = _version()
    prepared = _read_record(folder / _PREPARED, required=True)
    if prepared.get("schema") != _SCHEMA or prepared.get("manifest_sha256") != _manifest_hash(folder):
        raise PackInstallError("invalid_state", "The selected version's default preparation receipt changed.")
    return {"needed": "packs" in manifest and not _matches(manifest, _collection())}


def apply(*, previous: Path | None = None, boundary=None) -> dict:
    folder, manifest = _version()
    if "packs" not in manifest:
        return {"status": "legacy-no-default-list"}
    root = _collection()
    prepared = _read_record(folder / _PREPARED, required=True)
    if prepared.get("schema") != _SCHEMA or prepared.get("manifest_sha256") != _manifest_hash(folder):
        raise PackInstallError("invalid_release_manifest", "Prepare the selected release defaults before activation.")
    if previous is not None:
        previous = absolute(previous)
        if previous.parent != folder.parent:
            raise PackInstallError("invalid_release_manifest", "Previous release must belong to this installation.")
    receipt_path = folder / _RECEIPT
    prior_receipt = _read_record(receipt_path)
    if prior_receipt and prior_receipt.get("status") != "rolled_back":
        _validate_receipt(prior_receipt, folder, root, manifest)
        active = read_group(root)
        proposed = prior_receipt["publication"]
        repairing = previous == folder
        if repairing:
            # A new, explicitly requested repair of the selected release may
            # follow user group transactions. Defaults still honor tombstones
            # and cannot take over user/folder packs. An update recovery must
            # instead match its exact recorded publication below.
            installed_release_manifest()
        elif (active.transaction_id, active.revision) == (proposed["transaction_id"], proposed["revision"]):
            if not _matches(manifest, root):
                raise PackInstallError("pin_mismatch", "Published defaults changed; preserve them and retry the update explicitly.")
            prior_receipt["status"] = "applied"
            _write_record(receipt_path, prior_receipt)
            return prior_receipt
        elif (active.transaction_id, active.revision) != (proposed["previous_transaction_id"], proposed["previous_revision"]):
            raise PackInstallError("rollback_conflict", "The collection advanced outside this interrupted default delivery.")
    if _matches(manifest, root):
        return {"status": "unchanged", "manifest_sha256": _manifest_hash(folder)}
    record = {"schema": _SCHEMA, "status": "prepared", "manifest_sha256": _manifest_hash(folder),
              "collection": str(root), "previous": str(previous) if previous is not None else None,
              "previous_manifest_sha256": _manifest_hash(previous) if previous is not None else None}

    def before_publication(value):
        record["publication"] = dict(value)
        _write_record(receipt_path, record)

    result, proofs = _install(manifest, root, on_prepared=before_publication, boundary=boundary)
    record.update(status="applied", proofs=proofs)
    if (result.transaction_id, result.revision) != (record["publication"]["transaction_id"], record["publication"]["revision"]):
        raise PackInstallError("invalid_state", "Default publication differs from its prepared receipt.")
    _write_record(receipt_path, record)
    return record


def _validate_receipt(record: dict, folder: Path, root: Path, manifest: dict) -> None:
    fields = {"schema", "status", "manifest_sha256", "collection", "previous",
              "previous_manifest_sha256", "publication"}
    if (record.get("schema") != _SCHEMA or record.get("manifest_sha256") != _manifest_hash(folder)
            or record.get("collection") != str(root) or not fields <= set(record)
            or set(record) - fields - {"proofs", "rollback"}
            or record.get("status") not in {"prepared", "applied", "rolled_back"}):
        raise PackInstallError("invalid_state", "Default-delivery receipt disagrees with the installed version or collection.")
    try:
        if record["previous"] is None:
            if record["previous_manifest_sha256"] is not None:
                raise ValueError("Unexpected previous manifest digest.")
        elif (absolute(record["previous"]).parent != folder.parent
              or not re.fullmatch(r"[0-9a-f]{64}", record["previous_manifest_sha256"])):
            raise ValueError("Invalid previous version.")
        for kind in ("publication", "rollback"):
            if kind not in record:
                continue
            value = record[kind]
            if (not isinstance(value, dict) or set(value) != {"transaction_id", "revision", "previous_transaction_id",
                                                            "previous_revision", "pack_ids", "pins"}
                    or not re.fullmatch(r"[0-9a-f]{32}", value["transaction_id"])
                    or type(value["revision"]) is not int or type(value["previous_revision"]) is not int
                    or value["previous_revision"] < 0 or value["revision"] != value["previous_revision"] + 1
                    or (value["previous_transaction_id"] is None) != (value["previous_revision"] == 0)
                    or (value["previous_transaction_id"] is not None
                        and not re.fullmatch(r"[0-9a-f]{32}", value["previous_transaction_id"]))
                    or not isinstance(value["pack_ids"], list) or not value["pack_ids"]
                    or any(not isinstance(i, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", i)
                           or i in {"personal", "core", "astrid"} for i in value["pack_ids"])
                    or value["pack_ids"] != sorted(set(value["pack_ids"]))
                    or not isinstance(value["pins"], dict) or set(value["pins"]) != set(value["pack_ids"])):
                raise ValueError("Invalid prepared group identity.")
            for pack_id, pin in value["pins"].items():
                if pin is not None and _validate_pin(pin)["origin"] != "default":
                    raise ValueError("Default delivery cannot roll back a user pack.")
                if kind == "publication":
                    selected = manifest["packs"].get(pack_id)
                    expected = (None if selected is None else {"version": 1, "origin": "default",
                                "source": _git_source(selected["repo"]) + "#" + pack_id,
                                "commit": selected["commit"], "digest_rule": "pack-tree-v1", "digest": selected["digest"]})
                    if pin != expected:
                        raise ValueError("Publication differs from the selected source.")
            if kind == "rollback":
                applied = record["publication"]
                if (value["pack_ids"] != applied["pack_ids"]
                        or value["previous_transaction_id"] != applied["transaction_id"]
                        or value["previous_revision"] != applied["revision"]):
                    raise ValueError("Rollback differs from the applied transaction.")
    except (ValueError, TypeError, KeyError) as exc:
        raise PackInstallError("invalid_state", "The prepared default-delivery identity is invalid.") from exc


def rollback(*, previous: Path | None = None, boundary=None) -> dict:
    folder, manifest = _version()
    receipt_path = folder / _RECEIPT
    record = _read_record(receipt_path)
    if not record:
        return {"status": "not-applied"}
    root = _collection()
    _validate_receipt(record, folder, root, manifest)
    if (str(previous) if previous is not None else None) != record.get("previous"):
        raise PackInstallError("rollback_conflict", "The previous version differs from the prepared default delivery.")
    old_manifest = {}
    if previous is not None:
        previous = absolute(previous)
        if previous.parent != folder.parent or _manifest_hash(previous) != record.get("previous_manifest_sha256"):
            raise PackInstallError("rollback_conflict", "The retained previous release manifest changed.")
        old_manifest = load_release_manifest(previous / "manifest.json")
    active = read_group(root)
    proposed = record["publication"]
    active_identity = (active.transaction_id, active.revision)
    rollback_receipt = record.get("rollback")
    if (active_identity == (proposed["previous_transaction_id"], proposed["previous_revision"])
            or (rollback_receipt and active_identity == (rollback_receipt["transaction_id"], rollback_receipt["revision"]))):
        record["status"] = "rolled_back"
        _write_record(receipt_path, record)
        return record
    if active_identity != (proposed["transaction_id"], proposed["revision"]):
        raise PackInstallError("rollback_conflict", "The collection changed after default delivery; its state was preserved.")

    def before_publication(value):
        record["rollback"] = dict(value)
        _write_record(receipt_path, record)

    rollback_group(packs_root=root, transaction_id=proposed["transaction_id"],
                   pack_ids=proposed["pack_ids"], expected_pins=proposed["pins"],
                   expected_previous=(proposed["previous_transaction_id"], proposed["previous_revision"]),
                   release_pins=release_pins(old_manifest), python_packages=_packages(),
                   on_prepared=before_publication, boundary=boundary)
    record["status"] = "rolled_back"
    _write_record(receipt_path, record)
    return record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("prepare", "apply", "rollback", "needs-repair"))
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args()
    if args.operation == "prepare":
        value = prepare()
    elif args.operation == "needs-repair":
        value = needs_repair()
    else:
        value = (apply if args.operation == "apply" else rollback)(previous=args.previous)
    print(json.dumps(value, sort_keys=True))


if __name__ == "__main__":
    main()
