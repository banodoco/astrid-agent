"""Explicit pack installation and removal commands.

Mutations go through the M1-16 dependency-group transaction. This module only
collects source facts and obtains user consent; it never imports pack code.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from astrid.core.pack.canonical import validate_canonical_pack
from astrid.core.pack.identity import pack_digest, read_pin
from astrid.core.pack.install import PackInstallError, _directory, _fetch_tree, _git_source
from astrid.core.pack.personal import resolve_personal_packs_root
from astrid.core.pack.transaction import GroupCommitError, install_group


def _python_packages() -> dict[str, str]:
    packages: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            packages[name.lower().replace("_", "-").replace(".", "-")] = distribution.version
    return packages


def _preview(source: str, pack_id: str, subpath: str, revision: str | None) -> tuple[Path, str | None]:
    """Materialize a static preview using the installer’s source checks."""
    source_path = Path(source).expanduser()
    if revision is None:
        candidate = source_path / subpath
        try:
            _directory(candidate)
        except PackInstallError:
            raise PackInstallError("invalid_source", "Folder source must contain the selected pack directory.")
        return candidate, None

    work = Path(tempfile.mkdtemp(prefix="astrid-pack-consent-"))
    stage = work / pack_id
    stage.mkdir()
    try:
        resolved = _fetch_tree(work, _git_source(source), revision, subpath, stage)
        return stage, resolved
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


def _consent_details(pack_root: Path, *, source: str, commit: str | None) -> dict[str, Any]:
    manifest = validate_canonical_pack(pack_root).definition.to_dict()
    secrets = manifest.get("secrets", [])
    permissions = manifest.get("permissions", [])
    network = [permission for permission in permissions
               if isinstance(permission, dict) and permission.get("id") in {"network", "external_services"}]
    return {"id": manifest["id"], "name": manifest["name"], "source": source,
            "commit": commit, "secrets": secrets, "network": network}


def _show_consent(details: dict[str, Any]) -> None:
    print(f"Pack: {details['id']} ({details['name']})")
    print(f"Source: {details['source']}")
    print(f"Commit: {details['commit'] or '(local folder; unpinned)'}")
    print("Declared secrets:")
    print(json.dumps(details["secrets"], indent=2, sort_keys=True) if details["secrets"] else "  none")
    print("Declared network access:")
    print(json.dumps(details["network"], indent=2, sort_keys=True) if details["network"] else "  none")


def _confirm(prompt: str, *, yes: bool) -> bool:
    if yes:
        return True
    try:
        return input(prompt).strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def _handle_add(args: argparse.Namespace) -> int:
    pack_id = args.pack_id
    subpath = args.subpath
    source = args.source
    preview_root, commit = _preview(source, pack_id, subpath, args.revision)
    try:
        details = _consent_details(preview_root, source=source, commit=commit)
        if details["id"] != pack_id:
            raise PackInstallError("pack_id_mismatch", "Selected manifest id does not match --pack-id.")
        _show_consent(details)
        if not _confirm("Install this pack through the dependency-group transaction? [y/N] ", yes=args.yes):
            print("Pack installation cancelled.")
            return 1
        root = resolve_personal_packs_root(args.packs_root)
        if root is None:
            raise PackInstallError("invalid_packs_root", "Configure an absolute user packs root before installing.")
        result = install_group(source=source, revision=commit, packs={pack_id: subpath},
                              packs_root=root, python_packages=_python_packages(),
                              origin="folder" if commit is None else "user")
        print(f"Installed {pack_id} at {result.paths[pack_id]} (transaction {result.transaction_id}).")
        if result.refresh_pending:
            print("Refresh acknowledgment is pending; retry the committed transaction refresh.", file=sys.stderr)
        return 0
    finally:
        if args.revision is not None:
            shutil.rmtree(preview_root.parent, ignore_errors=True)


def _handle_remove(args: argparse.Namespace) -> int:
    root = resolve_personal_packs_root(args.packs_root)
    if root is None:
        raise PackInstallError("invalid_packs_root", "Configure an absolute user packs root before removing packs.")
    ids = tuple(dict.fromkeys(args.pack_ids))
    if not ids:
        raise PackInstallError("invalid_request", "Supply at least one pack id to remove.")
    print("Remove packs through the dependency-group transaction: " + ", ".join(ids))
    if not _confirm("Continue? [y/N] ", yes=args.yes):
        print("Pack removal cancelled.")
        return 1
    result = install_group(source=".", packs={}, packs_root=root,
                          python_packages=_python_packages(), remove=ids)
    print(f"Removed {', '.join(ids)} (transaction {result.transaction_id}).")
    if result.refresh_pending:
        print("Refresh acknowledgment is pending; retry the committed transaction refresh.", file=sys.stderr)
    return 0


def _handle_add_error(args: argparse.Namespace) -> int:
    try:
        return _handle_add(args)
    except (OSError, ValueError, GroupCommitError, subprocess.SubprocessError, yaml.YAMLError) as exc:
        print(f"packs add: {exc}", file=sys.stderr)
        return 1


def _handle_remove_error(args: argparse.Namespace) -> int:
    try:
        return _handle_remove(args)
    except (OSError, ValueError, GroupCommitError) as exc:
        print(f"packs remove: {exc}", file=sys.stderr)
        return 1


def build_add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("add", help="Review and install a pack through one transaction.")
    parser.add_argument("source", help="Folder or Git source containing the pack.")
    parser.add_argument("--pack-id", required=True, help="Pack identifier declared in pack.yaml.")
    parser.add_argument("--subpath", default=".", help="Pack path inside the source (default: .).")
    parser.add_argument("--revision", help="Git ref or commit to resolve and show before consent.")
    parser.add_argument("--packs-root", help="User pack collection root (defaults to ~/Astrid/packs).")
    parser.add_argument("--yes", action="store_true", help="Accept the displayed source and permission declarations.")
    parser.set_defaults(handler=_handle_add_error)


def build_remove_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("remove", help="Remove packs through one dependency-group transaction.")
    parser.add_argument("pack_ids", nargs="+", help="Pack identifiers to remove.")
    parser.add_argument("--packs-root", help="User pack collection root (defaults to ~/Astrid/packs).")
    parser.add_argument("--yes", action="store_true", help="Confirm the removal transaction.")
    parser.set_defaults(handler=_handle_remove_error)


def origin_and_modified(pack: Any) -> tuple[str, bool | None]:
    """Return installed provenance, with unknown modification for unpinned packs."""
    from astrid.core.pack.transaction import group_read_lock

    pin_path = pack.root / "PIN"
    try:
        pin_mode = pin_path.lstat().st_mode
    except FileNotFoundError:
        return str(getattr(pack, "origin", "unknown")), None
    if not stat.S_ISREG(pin_mode):
        return "unknown", None
    try:
        with group_read_lock(pack.root.parent):
            pin = read_pin(pin_path)
            origin = pin["origin"]
            expected = pin["digest"]
            modified = None if expected is None else pack_digest(pack.root) != expected
            return origin, modified
    except (OSError, ValueError):
        return "unknown", None
