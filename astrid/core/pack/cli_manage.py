"""Explicit pack installation and removal commands.

Mutations go through the M1-16 dependency-group transaction. This module only
collects source facts and obtains user consent; it never imports pack code.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from astrid.core.pack.canonical import validate_canonical_pack
from astrid.core.pack.identity import pack_digest, read_pin
from astrid.core.pack.install import PackInstallError
from astrid.core.pack.personal import resolve_personal_packs_root
from astrid.core.pack.transaction import GroupCommitError, install_group, rollback_group

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _consent_details(pack_root: Path, *, source: str, commit: str | None) -> dict[str, Any]:
    manifest = validate_canonical_pack(pack_root).definition.to_dict()
    secrets = manifest.get("secrets", [])
    actions = manifest.get("actions", {})
    action_secrets = {
        action_id: action["secrets"]
        for action_id, action in actions.items()
        if isinstance(action, dict) and action.get("secrets")
    }
    permissions = manifest.get("permissions", [])
    network = [permission for permission in permissions
               if isinstance(permission, dict) and permission.get("id") in {"network", "external_services"}]
    return {"id": manifest["id"], "name": manifest["name"], "source": source,
            "commit": commit, "secrets": secrets, "action_secrets": action_secrets,
            "network": network}


def _show_consent(details: dict[str, Any]) -> None:
    print(f"Pack: {details['id']} ({details['name']})")
    print(f"Source: {details['source']}")
    print(f"Commit: {details['commit'] or '(local folder; unpinned)'}")
    print("Declared secrets:")
    print(json.dumps(details["secrets"], indent=2, sort_keys=True) if details["secrets"] else "  none")
    print("Action-scoped secrets:")
    if details["action_secrets"]:
        for action_id, secrets in details["action_secrets"].items():
            print(f"  actions.{action_id}.secrets:")
            print(json.dumps(secrets, indent=2, sort_keys=True))
    else:
        print("  none")
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
    root = _selected_root(args.packs_root)

    def consent(changes: dict[str, Any]) -> bool:
        staged_root = Path(changes["packs"][pack_id]["staged_root"])
        details = _consent_details(staged_root, source=source, commit=changes["revision"])
        if details["id"] != pack_id:
            raise PackInstallError("pack_id_mismatch", "Selected manifest id does not match --pack-id.")
        _show_consent(details)
        accepted = _confirm("Install this exact staged pack through the dependency-group transaction? [y/N] ",
                             yes=args.yes)
        if accepted and args.revision is None:
            live_root = Path(source).expanduser() / subpath
            if pack_digest(live_root) != pack_digest(staged_root):
                raise PackInstallError("source_changed", "Pack source changed during consent; review it again.")
        return accepted

    root_was_present = root.exists()
    try:
        packages, pins = _validation_snapshot()
        result = install_group(source=source, revision=args.revision, packs={pack_id: subpath},
                              packs_root=root,
                              selected_roots=_composition_roots(root, include_default=args.packs_root is None),
                              core_root=_core_root(), python_packages=packages, release_pins=pins,
                              origin="folder" if args.revision is None else "user", consent=consent)
    except PackInstallError as exc:
        if exc.code in {"user_cancelled", "source_changed"} and not root_was_present:
            try:
                root.rmdir()
            except OSError:
                pass
        raise
    print(f"Installed {pack_id} at {result.paths[pack_id]} (transaction {result.transaction_id}).")
    if result.refresh_pending:
        print("Refresh acknowledgment is pending; retry the committed transaction refresh.", file=sys.stderr)
    return 0


def _handle_remove(args: argparse.Namespace) -> int:
    root = _selected_root(args.packs_root)
    ids = tuple(dict.fromkeys(args.pack_ids))
    if not ids:
        raise PackInstallError("invalid_request", "Supply at least one pack id to remove.")
    print("Remove packs through the dependency-group transaction: " + ", ".join(ids))
    if not _confirm("Continue? [y/N] ", yes=args.yes):
        print("Pack removal cancelled.")
        return 1
    packages, pins = _validation_snapshot()
    result = install_group(source=".", packs={}, packs_root=root,
                          selected_roots=_composition_roots(root, include_default=args.packs_root is None),
                          core_root=_core_root(), python_packages=packages, release_pins=pins, remove=ids)
    print(f"Removed {', '.join(ids)} (transaction {result.transaction_id}).")
    if result.refresh_pending:
        print("Refresh acknowledgment is pending; retry the committed transaction refresh.", file=sys.stderr)
    return 0


def _handle_add_error(args: argparse.Namespace) -> int:
    try:
        return _handle_add(args)
    except PackInstallError as exc:
        if exc.code == "user_cancelled":
            print("Pack installation cancelled.")
            return 1
        print(f"packs add: {exc}", file=sys.stderr)
        return 1
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
def _selected_root(override: str | None) -> Path:
    # ASTRID_PACKS_PATH extends discovery; it does not choose the collection
    # that management commands mutate. Only --packs-root may override the
    # default writable personal collection.
    default_root = Path.home() / "Astrid" / "packs"
    root = resolve_personal_packs_root(override if override is not None else default_root)
    if root is None:
        raise PackInstallError("unsafe_path", "Use an absolute writable collection with --packs-root.")
    return root


def _composition_roots(target_root: Path, *, include_default: bool) -> tuple[Path, ...]:
    """Freeze the host's explicitly selected external roots at CLI entry."""
    configured = os.environ.get("ASTRID_PACKS_PATH")
    values = ([Path.home() / "Astrid" / "packs"] if include_default else [])
    if configured is not None:
        values.extend(Path(item).expanduser() for item in configured.split(os.pathsep) if item)
    roots = []
    for value in values:
        if not value.is_absolute():
            raise PackInstallError("unsafe_path", "Selected external pack roots must be absolute paths.")
        resolved = value.resolve()
        if resolved != target_root.resolve():
            roots.append(resolved)
    return tuple(dict.fromkeys(roots))


def _core_root() -> Path:
    from .loader import packs_root
    return packs_root().resolve()


def _validation_snapshot() -> tuple[Mapping[str, str], Mapping[str, Mapping[str, str]]]:
    packages = {
        dist.metadata.get("Name", "").lower().replace("_", "-").replace(".", "-"): dist.version
        for dist in importlib.metadata.distributions() if dist.metadata.get("Name")
    }
    # Release policy is an independently supplied, verified snapshot. Source
    # declarations describe acquisition and may intentionally be unpinned.
    pins: dict[str, Mapping[str, str]] = {}
    pin_file = os.environ.get("ASTRID_RELEASE_PACK_PINS")
    if pin_file:
        try:
            raw_pins = json.loads(Path(pin_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PackInstallError("invalid_pin", "The authoritative release pack-pin snapshot is unreadable.") from exc
        if not isinstance(raw_pins, dict):
            raise PackInstallError("invalid_pin", "The authoritative release pack-pin snapshot must be an object.")
        for pack_id, value in raw_pins.items():
            if (not isinstance(pack_id, str) or not isinstance(value, dict)
                    or set(value) != {"commit", "digest"}
                    or not isinstance(value["commit"], str) or not _OBJECT_ID.fullmatch(value["commit"])
                    or not isinstance(value["digest"], str) or not _SHA256.fullmatch(value["digest"])
                    or value["commit"] != value["commit"].lower()):
                raise PackInstallError("invalid_pin", "Release pack pins require a full lowercase commit and tree digest.")
            pins[pack_id] = _freeze(dict(value))
    return MappingProxyType(packages), MappingProxyType(pins)


def _pack_specs(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        pack_id, separator, subpath = value.partition("=")
        if not separator or not pack_id or not subpath or pack_id in result:
            raise PackInstallError("invalid_request", "Select packs as unique ID=SUBPATH values.")
        result[pack_id] = subpath
    return result


def _confirm_update(changes: dict[str, Any]) -> bool:
    print("Proposed dependency group update:", file=sys.stderr)
    print(f"  source: {changes['source']}", file=sys.stderr)
    print(f"  target revision: {changes['revision']}", file=sys.stderr)
    for pack_id, change in changes["packs"].items():
        print(f"  {pack_id}: {change['before'] or 'not installed'} -> {change['after']}", file=sys.stderr)
        print(f"    tree: {change['before_digest'] or 'none'} -> {change['after_digest']}", file=sys.stderr)
    if changes.get("removed"):
        print(f"  removing: {', '.join(changes['removed'])}", file=sys.stderr)
    print(f"  preserved removals: {', '.join(changes['removals']) or 'none'}", file=sys.stderr)
    return input("Commit this validated group update? [y/N] ").strip().lower() in {"y", "yes"}


def _confirm_rollback(changes: dict[str, Any]) -> bool:
    print("Proposed dependency group rollback:", file=sys.stderr)
    for pack_id, change in changes["packs"].items():
        print(f"  {pack_id}: {change['current'] or 'not installed'} -> {change['restore'] or 'absent'}", file=sys.stderr)
        print(f"    tree: {change['current_digest'] or 'none'} -> {change['restore_digest'] or 'none'}", file=sys.stderr)
    print(f"  preserved removals: {', '.join(changes['removals']) or 'none'}", file=sys.stderr)
    return input("Commit this rollback? [y/N] ").strip().lower() in {"y", "yes"}


def _handle_update(args: argparse.Namespace) -> int:
    try:
        packs = _pack_specs(args.packs)
        root = _selected_root(args.packs_root)
        selected_roots = _composition_roots(root, include_default=args.packs_root is None)
        packages, pins = _validation_snapshot()
        result = install_group(source=args.source, revision=args.revision, packs=packs,
                               packs_root=root, selected_roots=selected_roots,
                               core_root=_core_root(),
                               python_packages=packages, release_pins=pins, intent="update",
                               consent=_confirm_update)
    except (PackInstallError, OSError, EOFError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not result.changed_ids:
        # Equal-byte update: nothing was published or retired.
        print(f"{', '.join(sorted(packs))} unchanged (generation {result.transaction_id}).")
        return 0
    print(f"Updated {', '.join(sorted(packs))} in committed generation {result.transaction_id}.")
    return 0


def _handle_rollback(args: argparse.Namespace) -> int:
    try:
        root = _selected_root(args.packs_root)
        selected_roots = _composition_roots(root, include_default=args.packs_root is None)
        packages, pins = _validation_snapshot()
        result = rollback_group(packs_root=root, pack_ids=args.pack_ids,
                                transaction_id=args.transaction_id, consent=_confirm_rollback,
                                selected_roots=selected_roots, core_root=_core_root(),
                                python_packages=packages, release_pins=pins)
    except (PackInstallError, OSError, EOFError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Rolled back {', '.join(sorted(set(args.pack_ids)))} in committed generation {result.transaction_id}.")
    return 0


__all__ = ["_handle_update", "_handle_rollback"]


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
